"""Manage a private local speech worker without loading GPU libraries into Qt."""
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import subprocess
import threading


ROOT = Path(__file__).resolve().parents[1]
STUDIO = ROOT / 'runtime/voice/voice-studio'
PYTHON = STUDIO / '.venv/Scripts/python.exe'
FAST_PYTHON = STUDIO / '.venv-fast/Scripts/python.exe'
WORKER = ROOT / 'tools/guga_voice_worker.py'
CLIPS = ROOT / 'resources/voice/guga'


def reference_audio():
    local = STUDIO / 'guga-reference.wav'
    return local if local.is_file() else CLIPS.parent / 'guga-reference.wav'


def prepared_clip(text):
    key = sha256(text.encode('utf-8')).hexdigest()[:16]
    return CLIPS / (key + '.wav')


def available():
    return all(path.is_file() for path in (
        PYTHON, PYTHON.parent.parent / 'Lib/site-packages/qwen_tts/__init__.py',
        reference_audio(), STUDIO / 'models/speech/model.safetensors',
        STUDIO / 'models/speech/speech_tokenizer/model.safetensors',
    ))


def fast_available():
    return FAST_PYTHON.is_file() and (
        FAST_PYTHON.parent.parent / 'Lib/site-packages/faster_qwen3_tts/__init__.py'
    ).is_file()


class GugaVoice:
    def __init__(self):
        self._process = None
        self._messages = None
        self._process_is_fast = False
        self._use_fast = True
        self._call_lock = threading.Lock()
        self._process_lock = threading.Lock()

    @staticmethod
    def _read(process, messages):
        try:
            for line in process.stdout:
                if line.startswith('GUGA_VOICE '):
                    messages.put(json.loads(line[len('GUGA_VOICE '):]))
        finally:
            messages.put({'exited': True})
            process.stdout.close()

    def _start(self):
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                return self._process, self._messages, False, self._process_is_fast
            if not available():
                raise RuntimeError('咕嘎专属声音模型或参考音频缺失')
            fast = self._use_fast and fast_available()
            command = [str(FAST_PYTHON if fast else PYTHON), '-u', str(WORKER)]
            if fast:
                command.append('--fast')
            process = subprocess.Popen(
                command, cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            messages = queue.Queue()
            self._process, self._messages = process, messages
            self._process_is_fast = fast
            threading.Thread(target=self._read, args=(process, messages), daemon=True).start()
            return process, messages, True, fast

    def synthesize(self, text, output, current=lambda: True, on_chunk=None):
        with self._call_lock:
            while current():
                emitted = False
                fast = self._use_fast and fast_available()
                try:
                    process, messages, starting, fast = self._start()
                    if starting and not messages.get(timeout=60).get('ready'):
                        raise RuntimeError('咕嘎专属声音启动失败')
                    if not current():
                        return False
                    request = dict(text=text, output=str(output), stream=fast and on_chunk is not None)
                    process.stdin.write(json.dumps(request, ensure_ascii=True) + '\n')
                    process.stdin.flush()
                    cancelled = False
                    while True:
                        result = messages.get(timeout=60)
                        if 'pcm' not in result:
                            if result.get('ok'):
                                return current()
                            raise RuntimeError('咕嘎专属声音合成失败')
                        if not current():
                            if not cancelled:
                                process.stdin.write('{"cancel":true}\n')
                                process.stdin.flush()
                                cancelled = True
                            continue
                        on_chunk(base64.b64decode(result['pcm'], validate=True), int(result['rate']))
                        emitted = True
                except (OSError, RuntimeError, ValueError, queue.Empty) as exc:
                    self.close()
                    if fast and not emitted:
                        self._use_fast = False
                        continue
                    if isinstance(exc, queue.Empty):
                        raise RuntimeError('咕嘎专属声音生成超时') from None
                    raise
            return False

    def close(self):
        with self._process_lock:
            process, self._process, self._messages = self._process, None, None
            if process is None:
                return
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
