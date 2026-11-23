# -*- coding: utf-8 -*-
"""咕嘎企鹅语音识别模块 — 麦克风录音 + Vosk 离线中文识别

- 依赖：sounddevice（录音）、vosk（识别）
- 模型：随项目携带 resources/models/vosk-model-small-cn-0.22
- 自动增益：录音时按峰值归一化（笔记本麦克风通常声音偏小）
- 用法：
    recorder = ASRRecorder()
    recorder.start()      # 开始录音（后台线程）
    recorder.stop()       # 停止并转写
    text = recorder.result
"""
import json
import logging
import os
import queue
import threading
import hashlib
import shutil
import tempfile
from pathlib import Path

# 模型下载地址（若缺失）
VOSK_MODEL_URL = 'https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip'

# 注意：vosk 的 C++ 引擎不支持非 ASCII 路径！
# 原始位置在用户目录下（可能含中文），实际加载必须用 ASCII 路径：
# 优先 %TEMP%\vosk-model-cn（复制品），否则用 8.3 短名转换。
_MODEL_SRC = os.path.join(os.environ.get('USERPROFILE', ''), '.zcode', 'tools', 'vosk-model-cn')
_BUNDLED_MODEL = Path(__file__).resolve().parents[1]/'resources/models/vosk-model-small-cn-0.22'

# 自动增益目标峰值（int16），太小听不清，太大削波
_GAIN_PEAK = 7000
_MAX_GAIN = 6.0


def _pick_input_device() -> int:
    """自动选择麦克风输入设备（优先名称含 麦克风/Microphone/Mic 的）"""
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        default_dev = sd.default.device
        default_in = None
        if default_dev is not None:
            try:
                default_in = int(default_dev[0])  # InputOutputPair / list
            except Exception:
                default_in = int(default_dev)
        candidates = []
        for i, d in enumerate(devs):
            if d['max_input_channels'] > 0:
                name = str(d['name']).lower()
                if any(k in name for k in ('麦克风', 'microphone', 'mic', 'audio in', 'capture')):
                    candidates.append(i)
        # 优先默认输入设备（若在候选里），否则第一个候选
        if default_in in candidates:
            return int(default_in)
        if candidates:
            return candidates[0]
        return int(default_in) if default_in is not None else 0
    except Exception:
        return 0


def _ascii_path(path: str) -> str:
    """把含中文的路径转成 8.3 短名（ASCII），失败返回原路径"""
    if path.isascii():
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
        if n and buf.value and buf.value.isascii():
            return buf.value
    except Exception:
        pass
    return path


def _valid_model(path):
    return all((Path(path)/name).is_file() for name in ('am/final.mdl','conf/model.conf','graph/HCLr.fst'))


def _ensure_model_dir() -> str:
    """Use the portable bundle; native Vosk gets an ASCII path even on Chinese desktops."""
    sources=[os.environ.get('GUGA_VOSK_MODEL'),str(_BUNDLED_MODEL),_MODEL_SRC,
             str(Path(tempfile.gettempdir())/'vosk-model-cn')]
    for value in sources:
        if not value or not _valid_model(value):continue
        source=Path(value)
        short=_ascii_path(str(source))
        if short.isascii():return short
        ident=hashlib.sha256((str(source.resolve())+str((source/'am/final.mdl').stat().st_mtime_ns)).encode()).hexdigest()[:16]
        for base in (os.environ.get('LOCALAPPDATA'),tempfile.gettempdir()):
            if not base:continue
            cache=Path(base)/'Guga/asr-cache'/ident
            short_cache=_ascii_path(str(cache.parent))
            if not short_cache.isascii():continue
            cache=Path(short_cache)/ident
            try:
                if not _valid_model(cache):
                    cache.parent.mkdir(parents=True,exist_ok=True)
                    staging=Path(tempfile.mkdtemp(prefix=ident+'-',dir=cache.parent))
                    try:
                        shutil.copytree(source,staging,dirs_exist_ok=True)
                        try:staging.rename(cache)
                        except FileExistsError:
                            if not _valid_model(cache):raise
                    finally:
                        # Only remove the unique staging directory we created inside our cache.
                        if staging.is_dir() and staging.resolve().parent==cache.parent.resolve():shutil.rmtree(staging)
                return str(cache)
            except OSError:continue
    return str(_BUNDLED_MODEL)


MODEL_DIR = _ensure_model_dir()
SENSE_VOICE_DIR = Path(__file__).resolve().parents[1] / 'runtime/voice/sense-voice'


class ASRRecorder:
    """按住说话式录音器（start 开始，stop 结束并转写）"""

    def __init__(self, *, prefer_sense_voice=False):
        self._q = queue.Queue()
        self._stream = None
        self._recording = False
        self._thread = None
        self._recognizer = None
        self._model = None
        self._sd = None
        self._vosk = None
        self._sense_voice = None
        self._prefer_sense_voice = prefer_sense_voice
        self.result = ''
        self.error = None
        self.device = None
        self._frame_count = 0
        self._peak = 0
        self.level = 0.
        self._start_time = 0.0
        self.backend = 'vosk'
        self._pcm = []

    @property
    def recognition_backend(self):
        return self.backend

    @property
    def available(self) -> bool:
        """依赖与模型是否就绪"""
        if not self._check_imports():
            return False
        if self._ensure_sense_voice():
            self.backend = 'sense_voice'
            return True
        from core.local_transcription import available
        if available():
            self.backend = 'whisper-small'
            return True
        self.backend = 'vosk'
        return self._ensure_model()

    def _check_imports(self):
        try:
            import sounddevice  # noqa
            self._sd = sounddevice
            try:
                import vosk
                self._vosk = vosk
            except ImportError:self._vosk=None
            return True
        except (ImportError,OSError):
            self.error='录音依赖不可用，请使用桌面文件夹内的“启动咕嘎.cmd”启动'
            return False

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if self._vosk is None:
            self.error = '缺少 Vosk 离线识别依赖'
            return False
        # 检查模型目录（解压后是 vosk-model-small-cn-0.22 子目录）
        if not os.path.isdir(MODEL_DIR):
            self.error='缺少本地中文识别模型，请将 Vosk 中文小模型放到 resources/models/vosk-model-small-cn-0.22'
            return False
        try:
            self._model = self._vosk.Model(MODEL_DIR)
            self.error = None
            return True
        except Exception:
            self.error='中文识别模型无法加载，请检查模型文件是否完整，以及模型缓存目录是否可写'
            return False

    def _ensure_sense_voice(self):
        if not self._prefer_sense_voice:
            return False
        if self._sense_voice is not None:
            return True
        model = SENSE_VOICE_DIR / 'model.int8.onnx'
        tokens = SENSE_VOICE_DIR / 'tokens.txt'
        if not (model.is_file() and tokens.is_file()):
            return False
        try:
            from sherpa_onnx import OfflineRecognizer

            self._sense_voice = OfflineRecognizer.from_sense_voice(
                model=str(model), tokens=str(tokens), num_threads=2, provider='cpu', language='auto',
            )
        except (ImportError, OSError, RuntimeError, ValueError):
            self._prefer_sense_voice = False
            return False
        return True

    def start(self):
        if self._recording or (self._thread and self._thread.is_alive()):
            return
        if not self.available:
            return
        self.result = ''
        self.error = None
        self._q=queue.Queue()
        self._recording = True
        self._pcm = []
        self._recognizer = self._vosk.KaldiRecognizer(self._model, 16000) if self.backend == 'vosk' else None
        self.device = _pick_input_device()
        self._frame_count = 0
        self._peak = 0
        self.level = 0.
        import time as _time
        self._start_time = _time.time()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def _record_loop(self):
        import sounddevice as sd
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16',
                                   channels=1, device=self.device, callback=self._callback):
                while self._recording:
                    self._accept(self._q.get())
        except Exception as e:
            self.error = f'录音设备错误: {e}'
        finally:
            self._recording=False
            # The stream is closed now: consume the final callback blocks before FinalResult.
            while not self._q.empty():self._accept(self._q.get_nowait())

    def _accept(self, data):
        if data is None:
            return
        if self.backend != 'vosk':
            self._pcm.append(data)
        elif self._recognizer.AcceptWaveform(data):
            self.result += json.loads(self._recognizer.Result()).get('text', '')

    def _transcribe_buffer(self, pcm):
        if not pcm:
            return ''
        if self.backend == 'sense_voice':
            try:
                import numpy as np

                samples = np.frombuffer(b''.join(pcm), dtype='<i2').astype(np.float32) / 32768.0
                stream = self._sense_voice.create_stream()
                stream.accept_waveform(16000, samples)
                self._sense_voice.decode_stream(stream)
                self.result = stream.result.text.strip()
                return self.result
            except (OSError, RuntimeError, ValueError):
                self._sense_voice = None
                self._prefer_sense_voice = False
        from core.local_transcription import available, transcribe

        if available():
            try:
                self.backend = 'whisper-small'
                self.result = transcribe(b''.join(pcm))
                return self.result.strip()
            except Exception as exc:
                logging.getLogger(__name__).info('Whisper unavailable, using Vosk: %s', type(exc).__name__)
        try:
            self.backend = 'vosk'
            self.result = self._transcribe_backup(pcm)
        except Exception:
            self.error = '离线语音识别暂不可用，请检查本地中文识别模型'
        return self.result.strip()

    def _callback(self, indata, frames, time_info, status):
        import numpy as np
        raw = bytes(indata)
        self._frame_count += frames
        try:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            peak = float(np.abs(arr).max()) if arr.size else 0.0
            self.level = min(1.,float(np.sqrt(np.mean(arr*arr)))/3500.) if arr.size else 0.
            if peak > self._peak:
                self._peak = peak
        except Exception:
            pass
        self._q.put(raw)

    def stop(self):
        """停止录音并完成转写（同步，返回文本）"""
        self._recording = False
        if self._thread:
            if self._thread.is_alive():
                self._q.put(None)  # Wake the consumer if Send falls between audio blocks.
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.error = '麦克风尚未释放，本轮没有提交识别结果'
                return ''
        pcm, self._pcm = self._pcm, []
        if self.error:
            return ''
        if self.backend != 'vosk':
            return self._transcribe_buffer(pcm)
        # 取残留
        if self._recognizer is not None:
            try:
                res = self._recognizer.FinalResult()
                import json as _json
                try:
                    txt = _json.loads(res).get('text', '')
                    if txt:
                        self.result += txt
                except Exception:
                    pass
            except Exception:
                pass
            self._recognizer=None
        return self.result.strip()

    def _transcribe_backup(self, pcm):
        if not self._ensure_model():
            raise RuntimeError('Vosk 中文模型未就绪')
        recognizer = self._vosk.KaldiRecognizer(self._model, 16000)
        phrases = []
        for block in pcm:
            if recognizer.AcceptWaveform(block):
                phrases.append(json.loads(recognizer.Result()).get('text', ''))
        phrases.append(json.loads(recognizer.FinalResult()).get('text', ''))
        return ' '.join(filter(None, phrases))

    def stats(self) -> dict:
        """录音统计（供 UI 提示）：时长秒、峰值、是否捕捉到声音"""
        import time as _time
        dur = _time.time() - self._start_time if self._start_time else 0.0
        return {
            'seconds': round(dur, 1),
            'peak': self._peak,
            'has_audio': self._peak > 500,   # 峰值低于此 = 基本没收到声音
        }


# 便捷函数
def transcribe_wav_path(wav_path: str) -> str:
    """离线转写 wav 文件（16k 单声道）"""
    import vosk
    if not os.path.isdir(MODEL_DIR):
        return ''
    model = vosk.Model(MODEL_DIR)
    rec = vosk.KaldiRecognizer(model, 16000)
    import wave
    with wave.open(wav_path, 'rb') as wf:
        if (wf.getnchannels(),wf.getsampwidth(),wf.getframerate())!=(1,2,16000):
            raise ValueError('转写需要 16kHz、16bit、单声道 WAV')
        parts=[]
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                import json
                parts.append(json.loads(rec.Result()).get('text',''))
    import json as _json
    try:
        return ''.join(parts+[_json.loads(rec.FinalResult()).get('text', '')])
    except Exception:
        return ''
