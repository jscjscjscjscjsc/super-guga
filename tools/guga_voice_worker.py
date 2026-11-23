"""Local Qwen3-TTS worker for Guga's original voice."""
import argparse
import base64
import json
import os
from pathlib import Path
import queue
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.guga_voice import STUDIO, reference_audio

REFERENCE_TEXT = '咕嘎，回来啦？外面风有点大，先坐会儿。喏，这杯是给你的。慢慢喝，今天还有好多事情想和你聊。'
DESCRIPTION = (
    '虚构桌宠咕嘎的原创年轻成年女声，标准普通话。声音明亮柔软，略有轻微气声和自然共鸣，'
    '中高音但不过分尖细。像熟悉的朋友当面聊天，带一点藏不住的笑意和俏皮，温柔亲近。'
    '语速中等，语句有松紧变化，停顿短而自然，句尾轻轻落下。避免播音腔、客服腔、'
    '夸张卖萌、耳语和拖音，保持清楚干净，录音室干声。'
)


def load_model(name):
    import torch
    from qwen_tts import Qwen3TTSModel

    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    dtype = torch.bfloat16 if device.startswith('cuda') else torch.float32
    return Qwen3TTSModel.from_pretrained(
        str(STUDIO / 'models' / name), device_map=device, dtype=dtype,
        attn_implementation='sdpa', local_files_only=True,
    )


def design(output, seed):
    import soundfile as sf
    import torch

    torch.manual_seed(seed)
    model = load_model('design')
    audio, rate = model.generate_voice_design(
        text=REFERENCE_TEXT, language='Chinese', instruct=DESCRIPTION, max_new_tokens=550,
    )
    sf.write(str(output), audio[0], rate, subtype='PCM_16')


def read_requests(requests):
    stop = None
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except ValueError:
                request = {}
            if request.get('cancel'):
                if stop is not None:
                    stop.set()
            else:
                stop = threading.Event()
                requests.put((request, stop))
    finally:
        if stop is not None:
            stop.set()
        requests.put(None)


def serve(fast=False):
    import soundfile as sf

    if fast:
        import torch
        from faster_qwen3_tts import FasterQwen3TTS

        model = FasterQwen3TTS.from_pretrained(str(STUDIO / 'models/speech'),
            device='cuda:0', dtype=torch.bfloat16, attn_implementation='sdpa')
        base = model.model
    else:
        model = base = load_model('speech')
    prompt = base.create_voice_clone_prompt(
        ref_audio=str(reference_audio()), ref_text=REFERENCE_TEXT,
    )
    requests = queue.Queue()
    threading.Thread(target=read_requests, args=(requests,), daemon=True).start()
    emit({'ready': True})
    for request, stop in iter(requests.get, None):
        try:
            text = request['text'].strip()
            if not text:
                raise ValueError('empty speech request')
            params = dict(
                text=text, language='Auto', voice_clone_prompt=prompt,
                max_new_tokens=min(1400, max(180, 105 + 9 * len(text))),
            )
            if fast and request.get('stream'):
                import numpy as np

                sent = False
                stream = model.generate_voice_clone_streaming(**params, chunk_size=6)
                try:
                    for audio, rate, _ in stream:
                        if stop.is_set():
                            break
                        if not audio.size:
                            continue
                        pcm = (np.clip(audio, -1., 1.) * 32767).astype('<i2').tobytes()
                        emit({'pcm': base64.b64encode(pcm).decode('ascii'), 'rate': int(rate)})
                        sent = True
                finally:
                    stream.close()
                if not sent and not stop.is_set():
                    raise ValueError('empty speech audio')
            else:
                audio, rate = model.generate_voice_clone(**params)
                sf.write(request['output'], audio[0], rate, subtype='PCM_16')
            emit({'ok': True})
        except Exception as exc:
            emit({'ok': False, 'error_type': type(exc).__name__})


def emit(value):
    print('GUGA_VOICE ' + json.dumps(value, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('OMP_NUM_THREADS', '4')
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--design', type=Path, metavar='WAV')
    parser.add_argument('--fast', action='store_true')
    parser.add_argument('--seed', type=int, default=15427)
    args = parser.parse_args()
    if args.design:
        design(args.design, args.seed)
    else:
        serve(fast=args.fast)
