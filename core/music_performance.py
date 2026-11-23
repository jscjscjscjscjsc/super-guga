"""Play completed covers and prepare new requests without blocking the desktop."""
import json
import subprocess
import sys
from pathlib import Path

from PyQt5.QtCore import QObject, QProcess, QTimer, QUrl, pyqtSignal
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer, QMediaPlaylist

from core.song_library import (
    MUSIC, canonical_title, is_cover, library_title, read_library as load_library, store_audio, write_song,
)

ROOT = Path(__file__).resolve().parents[1]
JOB_PREFIX = b'GUGA_SONG '


def read_library():
    return load_library(MUSIC)


def song_entry(title):
    library = read_library()
    entry = library.get(library_title(library, canonical_title(str(title))))
    return entry if isinstance(entry, dict) else None


def import_local_song(source, title='晴天'):
    title = canonical_title(str(title))
    if not title:
        raise ValueError('请填写歌曲名称')
    library = read_library()
    title = library_title(library, title)
    existing = library.get(title)
    original = store_audio(MUSIC, source)
    entry = original
    if is_cover(existing):
        old_original = existing.get('original')
        if not old_original or old_original.get('sha256') == original['sha256']:
            entry = dict(existing, original=original)
    write_song(MUSIC, title, entry, expected=existing)
    return entry


class MusicPerformance(QObject):
    performance_event = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.player.setVolume(70)
        self.playlist = QMediaPlaylist(self)
        self.player.setPlaylist(self.playlist)
        self.player.stateChanged.connect(self._state)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.error.connect(self._error)
        self.repeat = self.playing = self.requested = self.preparing = False
        self.title = self.source_kind = ''
        self.job = None
        self.job_output = b''
        self.job_result = None
        self._last_stage = (False, '')
        self.stage_sync = QTimer(self)
        self.stage_sync.setInterval(250)
        self.stage_sync.timeout.connect(self._sync_stage)

    def status(self):
        return dict(
            playing=self.playing, paused=self.player.state()==QMediaPlayer.PausedState,
            preparing=self.preparing, title=self.title, repeat=self.repeat,
            position_ms=self.player.position(), duration_ms=self.player.duration(),
        )

    def sing(self, title):
        title = canonical_title(str(title or '晴天'))
        if not title or len(title) > 120 or any(ord(char) < 32 for char in title):
            return dict(ok=False, error='请提供有效的歌曲名称')
        try:
            entry = song_entry(title)
            if is_cover(entry):
                path = MUSIC / entry.get('path', '')
                if path.is_file() and path.stat().st_size >= 1024:
                    return self._play(title, path)
        except (ValueError, OSError) as exc:
            return dict(ok=False, error=str(exc), title=title)
        return self.prepare(title)

    def _play(self, title, path):
        self.stop()
        self.title = title
        self.source_kind = 'guga_cover'
        self.requested = True
        self.repeat = title == '晴天'
        self.playlist.clear()
        if not self.playlist.addMedia(QMediaContent(QUrl.fromLocalFile(str(path.resolve())))):
            self.requested = False
            return dict(ok=False, error='无法载入已完成的咕嘎翻唱')
        self.playlist.setCurrentIndex(0)
        self.playlist.setPlaybackMode(QMediaPlaylist.CurrentItemInLoop if self.repeat else QMediaPlaylist.CurrentItemOnce)
        self.player.play()
        self.stage_sync.start()
        message = '已请求播放咕嘎翻唱'
        if self.repeat:
            message += '，循环播放直到停止唱歌'
        return dict(ok=True, accepted=True, playing=False, title=title, repeat=self.repeat,
                    source_kind=self.source_kind, message=message)

    def stop(self):
        self.stage_sync.stop()
        self.requested = self.repeat = False
        self.player.stop()
        self.playlist.setPlaybackMode(QMediaPlaylist.CurrentItemOnce)
        job, self.job = self.job, None
        was_preparing = self.preparing
        self.preparing = False
        if job is not None and job.state() != QProcess.NotRunning:
            if sys.platform == 'win32' and job.processId():
                try:
                    subprocess.run(
                        ['taskkill', '/PID', str(job.processId()), '/T', '/F'],
                        capture_output=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            job.kill()
        if was_preparing:
            self.performance_event.emit(dict(type='stopped', title=self.title))
        return dict(ok=True, message='已停止点歌')

    def pause(self):
        if not self.playing:
            return dict(ok=False, error='当前没有正在播放的歌曲')
        self.player.pause()
        self.performance_event.emit(dict(type='interaction', title=self.title))
        return dict(ok=True, message='已暂停歌曲，可以聊天。说继续唱歌从原处继续')

    def resume(self):
        if self.player.state() != QMediaPlayer.PausedState:
            return dict(ok=False, error='当前没有可继续的暂停歌曲')
        self.player.play()
        return dict(ok=True, accepted=True, message='已请求从暂停位置继续')

    def prepare(self, title):
        if self.preparing:
            if self.title == title:
                return dict(ok=True, accepted=True, execution_pending=True, message=f'《{title}》已经在制作队列中')
            return dict(ok=False, error=f'正在为《{self.title}》制作翻唱。可以等它完成，或先说“停止唱歌”。')
        worker = ROOT / 'tools/make_guga_cover.py'
        python = Path(sys.executable).with_name('python.exe') if sys.platform == 'win32' else Path(sys.executable)
        if not worker.is_file() or not python.is_file():
            return dict(ok=False, error='当前安装缺少本地翻唱程序')
        self.stop()
        self.title = title
        self.preparing = True
        job = QProcess(self)
        self.job = job
        self.job_result = None
        self.job_output = b''
        job.setWorkingDirectory(str(ROOT))
        job.setProcessChannelMode(QProcess.MergedChannels)
        job.readyReadStandardOutput.connect(lambda: self._read_job(job))
        job.finished.connect(lambda code, _: self._finish_job(job, code))
        job.errorOccurred.connect(lambda error: self._finish_job(job, -1) if error == QProcess.FailedToStart else None)
        message = f'正在为《{title}》寻找音源并制作咕嘎翻唱'
        self.performance_event.emit(dict(type='preparing', title=title, message=message))
        job.start(str(python), ['-u', str(worker), '--title='+title, '--discover', '--music-root', str(MUSIC)])
        return dict(ok=True, accepted=True, execution_pending=True, message=message)

    def _read_job(self, job):
        output = bytes(job.readAllStandardOutput())
        if job is not self.job:
            return
        lines = (self.job_output + output).split(b'\n')
        self.job_output = lines.pop()
        for line in lines:
            _, prefix, payload = line.rstrip(b'\r').rpartition(JOB_PREFIX)
            if not prefix:
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if 'ok' in event:
                self.job_result = event
            elif 'message' in event:
                self.performance_event.emit(dict(type='preparing', title=self.title, message=event['message']))

    def _finish_job(self, job, code):
        self._read_job(job)
        job.deleteLater()
        if job is not self.job:
            return
        result = self.job_result or {}
        self.job = None
        self.preparing = False
        if code == 0 and result.get('ok'):
            try:
                entry = song_entry(self.title)
                path = MUSIC / entry.get('path', '') if is_cover(entry) else None
                if path and path.is_file() and path.stat().st_size >= 1024:
                    played = self._play(self.title, path)
                    if played.get('ok'):
                        return
                    result = played
            except (OSError, ValueError) as exc:
                result = dict(error=str(exc))
        self.performance_event.emit(dict(
            type='error', title=self.title, error=result.get('error') or '本地翻唱程序未完成制作',
        ))

    def _state(self, state):
        active = state == QMediaPlayer.PlayingState
        self.playing = active
        self._sync_stage()
        if active:
            self.stage_sync.start()
        else:
            self.stage_sync.stop()

    def _sync_stage(self):
        current = (self.playing, self.title)
        if current != self._last_stage:
            self._last_stage = current
            self.performance_event.emit(dict(type='playing' if self.playing else 'stopped', title=self.title))

    def _media_status(self, status):
        if status == QMediaPlayer.EndOfMedia and not self.repeat:
            self.requested = False

    def _error(self, *_):
        self.requested = self.playing = False
        self.stage_sync.stop()
        self.performance_event.emit(dict(type='error', title=self.title, error=self.player.errorString()))
