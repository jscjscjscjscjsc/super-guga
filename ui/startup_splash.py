"""A local, skippable Guga opening with an identity-preserving dawn illustration.

Artwork and sound play from local files.
The elapsed-time choreography also drives the offline preview/export tool.
"""
import math
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QPointF, QRect, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap, QRadialGradient)
from PyQt5.QtWidgets import QApplication, QCheckBox, QPushButton, QWidget

WIDTH, HEIGHT = 1120, 700
DURATION = 7.4
LANDING = 2.16
SOUND_FILE = 'dawn-flight.wav'


def ease(value):
    value = min(1., max(0., value))
    return value * value * (3. - 2. * value)


@dataclass(frozen=True)
class FlightPose:
    x: float
    feet: float
    scale: float
    angle: float
    squash: float


def flight_at(seconds):
    """Bank in from the upper right, then land with feet anchored to the snow."""
    progress = max(0., min(1., (seconds - .26) / (LANDING - .26)))
    impact = max(0., min(1., (seconds - LANDING) / .64))
    recoil = math.sin(impact * 2 * math.pi) * (1. - impact)**2
    descent = .64 * progress + .36 * (1. - (1. - progress)**2.3)
    return FlightPose(
        x=739 + 565 * (1. - progress)**2.2,
        feet=168 + 470 * descent,
        scale=.34 + .66 * ease(progress),
        angle=24 * (1. - ease(progress)) - 5 * math.sin(progress * math.pi)**2,
        squash=1. - .12 * recoil,
    )


@dataclass(frozen=True)
class BootFrame:
    opacity: float
    title: str
    section: int


def frame_at(seconds):
    """Use wall time so a delayed paint never stretches the opening indefinitely."""
    t = max(0., min(DURATION, float(seconds)))
    section = 0 if t < 3.25 else 1 if t < 5.05 else 2
    titles = ('见到你，真好。', '今天，也一起。', '欢迎回来。')
    return BootFrame(1. - ease((t - 6.95) / .45), titles[section], section)


def fit_boot_rect(area):
    """Keep the whole opening inside the chosen monitor, including negative origins."""
    width = max(1, min(WIDTH, round(area.width() * .88),
                       round(area.height() * .88 * WIDTH / HEIGHT)))
    height = max(1, min(area.height(), round(width * HEIGHT / WIDTH)))
    return QRect(area.left() + (area.width() - width) // 2,
                 area.top() + (area.height() - height) // 2, width, height)


def startup_enabled(settings, argv=(), environ=None):
    import os
    env = os.environ if environ is None else environ
    if str(env.get('GUGA_STARTUP_ANIMATION', '')).lower() in ('0', 'false', 'off'):
        return False
    if any(flag in argv for flag in ('--no-intro', '--voice', '--realtime-voice',
                                    '--daily-plan', '--bg-live')):
        return False
    return settings.value('startup/animation', True, type=bool)


class GugaStartupSplash(QWidget):
    finished = pyqtSignal(str)

    def __init__(self, resource_dir, settings=None, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # Qt's Windows offscreen plugin has no system font database. Register
        # real local fonts for exports/tests; normal desktop startup needs none.
        if QApplication.platformName() == 'offscreen' and not QFontDatabase().families():
            import os
            font_dir = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
            for name in ('msyh.ttc', 'msyhbd.ttc', 'consola.ttf', 'segoeui.ttf'):
                if (font_dir/name).is_file():
                    QFontDatabase.addApplicationFont(str(font_dir/name))
        self.setWindowTitle('咕嘎 · 开场动画')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)
        self.settings = settings
        self.elapsed = 0.
        self._finished = True
        self._started = 0.
        startup_dir = Path(resource_dir) / 'startup'
        self._key_visual = QPixmap(str(startup_dir / 'guga-dawn-v2.png'))
        self._landscape = QPixmap(str(startup_dir / 'guga-dawn-plate.png'))
        self._character = QPixmap(str(startup_dir / 'guga-flight.png'))
        # Transparent padding in the supplied cutout is not part of the pose.
        # Keep the original alpha around the hair and rim lighting.
        self._character_source = QRectF(151, 27, 929, 1266)
        self._fallback_character = QPixmap(str(Path(resource_dir) / 'expressions/happy_transparent.png'))
        self._audio = None
        sound_path = startup_dir / SOUND_FILE
        if sound_path.is_file() and QApplication.platformName() != 'offscreen':
            from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
            self._audio = QMediaPlayer(self)
            self._audio.setVolume(45)
            self._audio.setMedia(QMediaContent(QUrl.fromLocalFile(str(sound_path.resolve()))))
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.skip = QPushButton('跳过   Esc', self)
        self.skip.setObjectName('startupSkip')
        self.skip.setCursor(Qt.PointingHandCursor)
        self.skip.clicked.connect(lambda: self.finish('skipped'))
        self.skip.setStyleSheet('QPushButton { color:#35495d; background:rgba(255,255,255,185); '
                                'border:1px solid rgba(255,255,255,210); border-radius:15px; '
                                'padding:5px 14px; font-family:"Microsoft YaHei UI"; }'
                                'QPushButton:hover { background:rgba(255,255,255,235); }'
                                'QPushButton:focus { border:2px solid #7893b5; }')
        self.next_time = QCheckBox('下次也播放', self)
        self.next_time.setObjectName('startupNextTime')
        self.next_time.setChecked(settings is None or settings.value('startup/animation', True, type=bool))
        self.next_time.setCursor(Qt.PointingHandCursor)
        self.next_time.setStyleSheet('QCheckBox { color:#35495d; background:rgba(255,255,255,160); '
                                     'border-radius:12px; padding:5px 10px; '
                                     'font-family:"Microsoft YaHei UI"; }')
        self.next_time.toggled.connect(self._save_enabled)
        self.sound = QCheckBox('开场音效', self)
        self.sound.setCursor(Qt.PointingHandCursor)
        self.sound.setStyleSheet(self.next_time.styleSheet())
        self.sound.toggled.connect(self._save_sound)
        self._refresh_options()
        self.resize(WIDTH, HEIGHT)

    @property
    def active(self):
        return not self._finished

    def _save_enabled(self, enabled):
        if self.settings is not None:
            self.settings.setValue('startup/animation', bool(enabled))
            self.settings.sync()

    def _refresh_options(self):
        if self.settings is None:
            self.sound.setChecked(True)
            return
        for control, key in ((self.next_time, 'startup/animation'), (self.sound, 'startup/sound')):
            blocked = control.blockSignals(True)
            control.setChecked(self.settings.value(key, True, type=bool))
            control.blockSignals(blocked)

    def _save_sound(self, enabled):
        if self.settings is not None:
            self.settings.setValue('startup/sound', bool(enabled))
            self.settings.sync()
        if self._audio is not None:
            self._audio.setMuted(not enabled)

    def play(self, screen=None):
        if self.active:
            self.raise_()
            return False
        if self.settings is not None:
            self._refresh_options()
        screen = screen or self.screen() or QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(fit_boot_rect(screen.availableGeometry()))
        self._finished = False
        self.elapsed = 0.
        self._started = time.monotonic()
        self.setWindowOpacity(1.)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        if self._audio is not None:
            self._audio.setMuted(not self.sound.isChecked())
            self._audio.setPosition(0)
            self._audio.play()
        self.timer.start()
        return True

    def finish(self, reason='finished'):
        if self._finished:
            return
        self._finished = True
        self.timer.stop()
        if self._audio is not None:
            self._audio.stop()
        self.hide()
        self.finished.emit(reason)

    def _tick(self):
        self.elapsed = max(0., time.monotonic() - self._started)
        if self.elapsed >= DURATION:
            self.finish()
            return
        if QApplication.platformName() != 'offscreen':
            self.setWindowOpacity(frame_at(self.elapsed).opacity)
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.finish('skipped')
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.finish('skipped')
        event.accept()

    def resizeEvent(self, event):
        scale = min(self.width() / WIDTH, self.height() / HEIGHT)
        self.skip.setGeometry(round(self.width() - 154 * scale), round(26 * scale),
                              max(100, round(126 * scale)), max(28, round(34 * scale)))
        self.next_time.setGeometry(round(30 * scale), round(self.height() - 51 * scale),
                                   max(124, round(145 * scale)), max(25, round(29 * scale)))
        self.sound.setGeometry(self.next_time.geometry().right() + max(7, round(10 * scale)),
                               self.next_time.y(), max(100, round(114 * scale)), self.next_time.height())
        font = QFont('Microsoft YaHei UI')
        font.setPixelSize(max(11, round(12 * scale)))
        self.skip.setFont(font)
        self.next_time.setFont(font)
        self.sound.setFont(font)
        super().resizeEvent(event)

    @staticmethod
    def _text(painter, x, y, text, size, color, weight=QFont.Normal, mono=False, spacing=0.):
        font = QFont('Consolas' if mono else 'Microsoft YaHei UI')
        font.setPixelSize(size)
        font.setWeight(weight)
        if spacing:
            font.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(QPointF(x, y), text)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.paint_scene(painter, self.width(), self.height(), self.elapsed)
        painter.end()

    def render_frame(self, seconds, width=WIDTH, height=HEIGHT, *, fade_out=False):
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        self.paint_scene(painter, width, height, seconds)
        if fade_out:
            painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
            painter.fillRect(image.rect(), QColor(240,244,252,round(255*(1-frame_at(seconds).opacity))))
        painter.end()
        return image

    def paint_scene(self, p, width, height, seconds):
        t = max(0., float(seconds))
        state = frame_at(t)
        p.save()
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing |
                         QPainter.SmoothPixmapTransform)
        p.scale(width / WIDTH, height / HEIGHT)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, WIDTH, HEIGHT), 22, 22)
        p.setClipPath(clip)
        p.fillRect(0, 0, WIDTH, HEIGHT, QColor('#e9edf4'))
        if not self._landscape.isNull() and not self._character.isNull():
            self._draw_flight(p, t)
        elif self._key_visual.isNull():
            self._draw_fallback(p, t)
        else:
            self._draw_film(p, t)
        self._draw_snow(p, t)
        self._draw_titles(p, t, state)
        p.restore()

    def _draw_film(self, p, t):
        self._draw_landscape(p, self._key_visual, t)

    def _draw_flight(self, p, t):
        pose = flight_at(t)
        self._draw_landscape(p, self._landscape, t)
        self._draw_air_trail(p, t, pose)
        height = 623 * pose.scale
        width = height * self._character_source.width() / self._character_source.height()
        gap = 638 - pose.feet
        if gap < 300:
            shadow = QRadialGradient(0, 0, 1)
            shadow.setColorAt(0, QColor(74, 88, 138, round(124 * (1. - gap/300))))
            shadow.setColorAt(.62, QColor(96, 112, 162, round(78 * (1. - gap/300))))
            shadow.setColorAt(1, QColor(134, 145, 194, 0))
            p.save()
            p.translate(pose.x + 23, 635)
            p.scale(154 + gap*.12, 21 + gap*.04)
            p.setPen(Qt.NoPen)
            p.setBrush(shadow)
            p.drawEllipse(QPointF(), 1, 1)
            p.restore()
        p.save()
        p.translate(pose.x, pose.feet)
        p.rotate(pose.angle)
        p.scale(1. + (1. - pose.squash)*.52, pose.squash)
        p.drawPixmap(QRectF(-width/2, -height, width, height), self._character, self._character_source)
        p.restore()
        self._draw_landing_snow(p, t)

    @staticmethod
    def _draw_air_trail(p, t, pose):
        visibility = ease((t - .62) / .37) * (1. - ease((t - 1.38) / .45))
        if visibility == 0.:
            return
        p.save()
        for layer in range(3):
            x, y = pose.x + 92 - layer * 26, pose.feet - 43 - layer * 30
            trail = QPainterPath(QPointF(x, y))
            trail.quadTo(x + 92, y - 17 - layer * 8, x + 235, y - 106)
            fade = QLinearGradient(x, y, x + 235, y - 106)
            fade.setColorAt(0, QColor(255, 250, 238, round(122 * visibility)))
            fade.setColorAt(.66, QColor(255, 247, 237, round(57 * visibility)))
            fade.setColorAt(1, QColor(255, 247, 237, 0))
            p.setPen(QPen(fade, 2.6 - layer*.55, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(trail)
        p.restore()

    def _draw_landing_snow(self, p, t):
        elapsed = t - LANDING
        if not 0 < elapsed < 1.05:
            return
        p.save()
        p.setPen(Qt.NoPen)
        for index in range(25):
            side = -1 if index % 2 else 1
            speed = 47 + (index*37) % 164
            x = 739 + (index*29 % 158 - 79) + side * speed * elapsed
            lift = (36 + (index*31) % 104)*elapsed - 98*elapsed**2
            y = min(659, 638 + (index % 6)*3 - lift)
            radius = (2. + index % 5) * (1. - elapsed*.6)
            alpha = (1. - ease(elapsed / 1.05)) * ease(elapsed / .09)
            p.setBrush(QColor(249, 250, 255, round(202 * alpha)))
            p.drawEllipse(QPointF(x, y), radius * 1.35, radius)
        p.restore()

    def _draw_landscape(self, p, image, t):
        pullback = ease(t / 4.8)
        zoom = 1.04 - .04 * pullback
        ratio = WIDTH / HEIGHT
        crop_w = min(float(image.width()), image.height() * ratio) / zoom
        crop_h = crop_w / ratio
        center_x = image.width() * (.515 - .015 * pullback)
        left = max(0., min(image.width() - crop_w, center_x - crop_w/2))
        top = max(0., (image.height() - crop_h)*.08)
        p.drawPixmap(QRectF(0, 0, WIDTH, HEIGHT), image,
                     QRectF(left, top, crop_w, crop_h))
        # A restrained veil supports readable type while keeping the sky visible.
        veil = QLinearGradient(0, 0, WIDTH*.53, 0)
        veil.setColorAt(0, QColor(250, 245, 245, 55))
        veil.setColorAt(.7, QColor(250, 245, 245, 12))
        veil.setColorAt(1, QColor(250, 245, 245, 0))
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), veil)

    def _draw_fallback(self, p, t):
        # Keep the actual Guga character if a copied installation loses its scene.
        sky = QLinearGradient(0, 0, WIDTH, HEIGHT)
        sky.setColorAt(0, QColor('#fae9e4'))
        sky.setColorAt(.55, QColor('#e4e7f4'))
        sky.setColorAt(1, QColor('#cee3f1'))
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), sky)
        glow = QRadialGradient(820, 310, 340)
        glow.setColorAt(0, QColor(255, 251, 240, 210))
        glow.setColorAt(1, QColor(255, 251, 240, 0))
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), glow)
        image = self._fallback_character
        if image.isNull():
            return
        height = 545 + 8*math.sin(t*.7)
        w = height * image.width() / image.height()
        p.drawPixmap(QRectF(804-w/2, 632-height, w, height), image, QRectF(image.rect()))

    def _draw_titles(self, p, t, state):
        p.save()
        reveal = ease((t-1.53)/.71)
        p.setOpacity(reveal)
        y = 301 + 10*(1-reveal)
        self._text(p, 81, y, '咕嘎', 65, '#35465f', QFont.DemiBold, spacing=7)
        p.setPen(QPen(QColor(173, 138, 134, 165), 1.5))
        p.drawLine(QPointF(87, y+28), QPointF(125, y+28))
        section_start = (2.22, 3.25, 5.05)[state.section]
        subtitle_alpha = ease((t-section_start)/.5)
        p.setOpacity(reveal*subtitle_alpha)
        self._text(p, 85, y+72+5*(1-subtitle_alpha), state.title, 20, '#52617b')
        p.restore()

    def _draw_snow(self, p, t):
        p.save()
        p.setPen(Qt.NoPen)
        for i in range(24):
            x = (i*137.7 + math.sin(t*.35+i)*17 - t*(4+i%3)) % (WIDTH+80) - 40
            y = (i*83.1 + t*(8+i%4)) % (HEIGHT+80) - 40
            radius = .9 + (i%4)*.65
            p.setBrush(QColor(255, 254, 250, 70 + (i*17)%95))
            p.drawEllipse(QPointF(x,y), radius, radius)
        # Near-camera bokeh moves slowly across a different plane from the snow.
        for i in range(5):
            x = (i*263.3 - t*14) % (WIDTH+100) - 50
            y = 85 + (i*131.7 + math.sin(t*.3+i)*15) % 550
            radius = 10 + i*2
            glow = QRadialGradient(x, y, radius)
            glow.setColorAt(0, QColor(255, 250, 238, 40))
            glow.setColorAt(1, QColor(255, 250, 238, 0))
            p.setBrush(glow)
            p.drawEllipse(QPointF(x,y), radius, radius)
        p.restore()
