"""Convert a separated singing track to the same reference used by local TTS."""
import argparse
from pathlib import Path
import os
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.guga_voice import STUDIO, reference_audio

MODELS = STUDIO / 'models/sing'
SEED_VC = STUDIO / 'seed-vc'
CHECKPOINT = MODELS / 'DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth'
CONFIG = MODELS / 'config_dit_mel_seed_uvit_whisper_base_f0_44k.yml'


def convert(source, output, steps=32, octave=0, start=0., duration=None):
    import numpy as np
    import soundfile as sf
    import torch
    import yaml

    if not torch.cuda.is_available():
        raise RuntimeError('歌声转换需要可用的 CUDA 环境')
    os.environ['HF_HUB_OFFLINE'] = '1'
    sys.path.insert(0, str(SEED_VC))
    import inference

    def local_weight(repo, filename, config_filename=None):
        path = MODELS / filename
        if not path.is_file() or config_filename:
            raise FileNotFoundError(f'缺少本地模型文件 {filename}')
        return str(path)

    source = Path(source).resolve()
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    reference = reference_audio()
    voice, rate = sf.read(str(source), dtype='float32', always_2d=True)
    if rate != 44100:
        raise ValueError('歌声输入必须为 44.1 kHz')
    start_frame = round(start * rate)
    last_frame = len(voice) if duration is None else start_frame + round(duration * rate)
    voice = voice[start_frame:last_frame].mean(axis=1)
    if not len(voice):
        raise ValueError('选定的歌曲范围没有音频')
    mixed = np.zeros_like(voice)
    overlap = round(.4 * rate)
    length = round(23.5 * rate)
    hop = length - overlap
    config = yaml.safe_load(CONFIG.read_text(encoding='utf-8'))
    config['model_params']['vocoder']['name'] = str(MODELS / 'bigvgan')
    config['model_params']['speech_tokenizer']['name'] = str(MODELS / 'whisper')
    inference.load_custom_model_from_hf = local_weight
    args = argparse.Namespace(
        target=str(reference), diffusion_steps=steps, length_adjust=1.0,
        inference_cfg_rate=.72, f0_condition=True, auto_f0_adjust=False,
        semi_tone_shift=octave * 12, checkpoint=str(CHECKPOINT), fp16=True,
    )
    with tempfile.TemporaryDirectory(prefix='guga-singing-', dir=STUDIO) as temporary:
        folder = Path(temporary)
        config_path = folder / 'local-config.yml'
        config_path.write_text(yaml.safe_dump(config), encoding='utf-8')
        args.config = str(config_path)
        args.output = str(folder)
        args.source = str(folder / 'vocal.wav')
        loaded = inference.load_models(args)
        inference.load_models = lambda _: loaded
        for offset in range(0, len(voice), hop):
            part = voice[offset:offset + length]
            print(f'转换人声 {offset/rate:.1f}/{len(voice)/rate:.1f} 秒', flush=True)
            if np.sqrt(np.mean(part**2)) < .0015:
                generated = np.zeros_like(part)
            else:
                # Pad the last sliver so pitch extraction still has enough context.
                padded = np.pad(part, (0, max(0, rate - len(part))))
                sf.write(args.source, padded, rate)
                inference.main(args)
                result = next(folder.glob('vc_vocal_*.wav'))
                generated, output_rate = sf.read(str(result), dtype='float32')
                result.unlink()
                if output_rate != rate or abs(len(generated) - len(padded)) > rate // 2:
                    raise RuntimeError('换声后人声与伴奏时长不一致')
                generated = np.pad(generated[:len(part)], (0, max(0, len(part) - len(generated))))
            # A short overlap preserves attacks and prevents audible chunk edges.
            if offset:
                width = min(overlap, len(generated))
                blend = np.linspace(0, 1, width, dtype=np.float32)
                generated[:width] = (1-blend)*mixed[offset:offset + width] + blend*generated[:width]
            mixed[offset:offset + len(part)] = generated
            if offset + len(part) == len(voice):
                break
    if not np.isfinite(mixed).all() or np.sqrt(np.mean(mixed**2)) < .0002:
        raise RuntimeError('换声结果没有可用人声')
    # Suppress conversion noise in rests, using the separated vocal's envelope.
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    stride = round(.030 * rate)
    blocks = np.pad(voice, (0, (-len(voice)) % stride)).reshape(-1, stride)
    energy = np.sqrt(np.mean(blocks**2, axis=1))
    quiet = max(.0018, float(np.percentile(energy, 90)) * .034)
    gain = np.clip((energy - quiet) / (3.0*quiet), 0., 1.)
    gain = uniform_filter1d(maximum_filter1d(gain, size=5), size=5)
    mixed *= np.interp(np.arange(len(voice)), (np.arange(len(gain)) + .5) * stride, gain)
    sf.write(str(output), mixed, rate)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vocals', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--steps', type=int, default=32)
    parser.add_argument('--octave', type=int, choices=(-1, 0, 1), default=0)
    parser.add_argument('--start', type=float, default=0.)
    parser.add_argument('--duration', type=float)
    args = parser.parse_args()
    convert(args.vocals, args.output, args.steps, args.octave, args.start, args.duration)
