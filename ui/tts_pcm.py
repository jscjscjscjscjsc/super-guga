"""Play incremental mono PCM on the Qt thread."""
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtMultimedia import QAudio, QAudioDeviceInfo, QAudioFormat, QAudioOutput


def speech_format():
    audio = QAudioFormat()
    audio.setCodec('audio/pcm')
    audio.setSampleRate(24000)
    audio.setChannelCount(1)
    audio.setSampleSize(16)
    audio.setByteOrder(QAudioFormat.LittleEndian)
    audio.setSampleType(QAudioFormat.SignedInt)
    return audio


class SpeechPcmOutput(QObject):
    playing = pyqtSignal()
    finished = pyqtSignal()
    failed = pyqtSignal()

    def __init__(self, player, parent):
        super().__init__(parent)
        self._player = player
        self._output = self._device = None
        self._pending = bytearray()
        self._done = self._started = False
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._write)
        player.volumeChanged.connect(self._set_volume)
        player.mutedChanged.connect(self._set_volume)

    @staticmethod
    def available():
        device = QAudioDeviceInfo.defaultOutputDevice()
        return not device.isNull() and device.isFormatSupported(speech_format())

    @property
    def active(self):
        return self._output is not None

    def append(self, packet):
        if self._output is None:
            self._output = QAudioOutput(speech_format(), self)
            self._output.setBufferSize(9600)
            self._set_volume()
            self._device = self._output.start()
            if self._device is None:
                self.stop()
                self.failed.emit()
                return
            self._timer.start()
        self._pending.extend(packet)
        self._write()

    def finish(self):
        self._done = True

    def stop(self):
        self._timer.stop()
        if self._output is not None:
            self._output.stop()
            self._output.deleteLater()
        self._output = self._device = None
        self._pending.clear()
        self._done = self._started = False

    def _set_volume(self):
        if self._output is not None:
            volume = 0.0 if self._player.isMuted() else self._player.volume() / 100.0
            self._output.setVolume(volume)

    def _write(self):
        output = self._output
        if output is None:
            return
        if output.error() not in (QAudio.NoError, QAudio.UnderrunError) or output.state() == QAudio.StoppedState:
            self.stop()
            self.failed.emit()
            return
        count = min(len(self._pending), output.bytesFree()) & ~1
        if count > 0:
            written = self._device.write(bytes(self._pending[:count]))
            if written < 0:
                self.stop()
                self.failed.emit()
            elif written:
                del self._pending[:written]
                if not self._started:
                    self._started = True
                    self.playing.emit()
        elif self._done and not self._pending and output.state() == QAudio.IdleState:
            self.stop()
            self.finished.emit()
