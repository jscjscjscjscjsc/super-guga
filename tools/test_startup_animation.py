"""Offline boot, escape/handoff, settings and raster regression checks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from launcher import _prepare_qt
_prepare_qt()
from PyQt5.QtCore import QSettings, QRect, Qt, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from ui.startup_splash import DURATION, GugaStartupSplash, fit_boot_rect, frame_at, startup_enabled

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class BootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name)/'startup.ini'), QSettings.IniFormat)
        self.splash = GugaStartupSplash(ROOT/'resources', self.settings)

    def tearDown(self):
        self.splash.finish('test')
        self.splash.hide()
        self.splash.deleteLater()
        self.temp.cleanup()

    def test_complete_sequence_changes_captions_and_fades_out(self):
        frames = [frame_at(n/10) for n in range(80)]
        self.assertEqual(frames[0].opacity, 1.)
        self.assertEqual(frame_at(2.5).section, 0)
        self.assertEqual(frame_at(4.).section, 1)
        self.assertEqual(frame_at(6.).title, '欢迎回来。')
        self.assertEqual(frame_at(DURATION).opacity, 0.)
        self.assertEqual(frame_at(1000).opacity, 0.)
        self.assertTrue(all(a.section <= b.section for a,b in zip(frames,frames[1:])))

    def test_window_fits_small_hidpi_and_negative_origin_monitors(self):
        for area in (QRect(0,0,800,600), QRect(-1920,-200,1920,1080),
                     QRect(2560,0,3840,2160), QRect(0,0,400,240)):
            with self.subTest(area=area):
                fitted = fit_boot_rect(area)
                self.assertTrue(area.contains(fitted))
                self.assertLess(abs(fitted.width()/fitted.height()-1.6), .02)

    def test_skip_escape_and_close_finish_exactly_once(self):
        for action in ('button','escape','close'):
            with self.subTest(action=action):
                done = []
                self.splash.finished.connect(done.append)
                self.splash.play()
                if action == 'button': self.splash.skip.click()
                elif action == 'escape': QTest.keyClick(self.splash, Qt.Key_Escape)
                else: self.splash.close()
                self.splash.finish()
                self.assertEqual(done, ['skipped'])
                self.assertFalse(self.splash.timer.isActive())
                self.assertFalse(self.splash.isVisible())
                self.splash.finished.disconnect(done.append)

    def test_delayed_event_loop_finishes_instead_of_extending_startup(self):
        done = []
        self.splash.finished.connect(done.append)
        with patch('ui.startup_splash.time.monotonic',return_value=100.):
            self.splash.play()
        with patch('ui.startup_splash.time.monotonic',return_value=150.):
            self.splash._tick()
        self.assertEqual(done,['finished'])
        self.assertFalse(self.splash.active)

    def test_opt_out_persists_and_workflow_launches_bypass_opening(self):
        self.assertTrue(startup_enabled(self.settings,[],{}))
        self.splash.next_time.setChecked(False)
        reopened = QSettings(self.settings.fileName(),QSettings.IniFormat)
        self.assertFalse(startup_enabled(reopened,[],{}))
        self.splash.next_time.setChecked(True)
        for flag in ('--no-intro','--voice','--realtime-voice','--daily-plan','--bg-live'):
            self.assertFalse(startup_enabled(reopened,[flag],{}))
        self.assertFalse(startup_enabled(reopened,[],{'GUGA_STARTUP_ANIMATION':'off'}))

    def test_flight_enters_visible_snow_scene_and_lands_with_chinese_title(self):
        self.assertFalse(self.splash._landscape.isNull())
        self.assertTrue(self.splash._character.hasAlphaChannel())
        first = self.splash.render_frame(.05)
        airborne = self.splash.render_frame(1.46)
        settled = self.splash.render_frame(4.5)
        def dark_pixels(image, left, top, right, bottom):
            return sum(image.pixelColor(x,y).lightness()<110
                       for y in range(top,bottom,3) for x in range(left,right,3))
        self.assertLess(dark_pixels(first,600,20,1080,320),50)
        self.assertGreater(dark_pixels(airborne,650,100,1080,400),500)
        self.assertLess(dark_pixels(airborne,580,550,935,610),30)
        self.assertGreater(dark_pixels(settled,580,550,935,610),240)
        image = self.splash.render_frame(5.8)
        # Catch the Windows offscreen plugin's missing-font failure: the left
        # headline must contain dark letterforms, not just a pale empty panel.
        ink = sum(image.pixelColor(x,y).lightness()<110
                  for y in range(220,300,2) for x in range(80,300,2))
        self.assertGreater(ink,500)
        self.assertEqual(image.pixelColor(0,0).alpha(),0)

    def test_missing_illustration_still_shows_the_character(self):
        from PyQt5.QtGui import QPixmap
        self.splash._key_visual = QPixmap()
        self.splash._landscape = QPixmap()
        first = self.splash.render_frame(.05)
        hood = [first.pixelColor(x,y) for y in range(90,330,3) for x in range(670,950,3)]
        self.assertGreater(sum(color.lightness()<110 for color in hood),300)

    def test_sound_preference_persists_and_finishing_stops_playback(self):
        from unittest.mock import Mock
        audio = self.splash._audio = Mock()
        self.splash.sound.setChecked(False)
        self.assertFalse(self.settings.value('startup/sound', True, type=bool))
        self.splash.play()
        audio.setMuted.assert_called_with(True)
        self.splash.sound.setChecked(True)
        audio.setMuted.assert_called_with(False)
        self.splash.skip.click()
        audio.stop.assert_called_once()


class PetHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temp.name)
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,cls.temp.name)
        from core.agent_memory import AgentMemoryStore
        original_store = AgentMemoryStore
        cls.patches = [patch('core.keyboard_hook.KeyboardHook.start'),
                       patch('ui.tts_engine.TTSEngine.speak'),
                       patch('ui.mcp_bridge.SelfMCPBridge'),
                       patch('core.agent_memory.AgentMemoryStore',
                             side_effect=lambda path=None:original_store(path or cls.folder/'memory.sqlite3'))]
        for item in cls.patches:item.start()
        from ui.pet_window import PetWindow
        cls.pet = PetWindow()
        for timer in cls.pet.findChildren(QTimer):timer.stop()
        cls.pet._anim.stop()

    @classmethod
    def tearDownClass(cls):
        cls.pet._cancel_startup_animation('test')
        for timer in cls.pet.findChildren(QTimer):timer.stop()
        cls.pet._anim.stop()
        cls.pet._screen_tools.stop()
        cls.pet._self_mcp.close()
        cls.pet._tray.hide()
        if cls.pet._pet_menu:cls.pet._pet_menu.hide()
        cls.pet.hide()
        for item in reversed(cls.patches):item.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.pet._cancel_startup_animation('test')
        self.pet._desktop_hidden = False
        self.pet._config._s.setValue('startup/animation',True)
        self.pet.show()
        self.pet._welcome_timer.stop()

    def tearDown(self):
        self.pet._cancel_startup_animation('test')
        self.pet._welcome_timer.stop()

    def test_start_once_skip_restores_pet_and_delays_original_greeting(self):
        self.pet._welcome_timer.start(1500)
        self.assertTrue(self.pet.play_startup_animation(automatic=True))
        splash = self.pet._startup_splash
        self.assertFalse(self.pet.play_startup_animation(automatic=True))
        self.assertTrue(self.pet._startup_splash_active)
        self.assertFalse(self.pet._welcome_timer.isActive())
        splash.skip.click()
        self.assertFalse(self.pet._startup_splash_active)
        self.assertTrue(self.pet.isVisible())
        self.assertTrue(self.pet._welcome_timer.isActive())
        self.assertEqual(self.pet._welcome_timer.interval(),250)

    def test_disabled_startup_can_still_be_replayed_manually(self):
        self.pet._config._s.setValue('startup/animation',False)
        self.assertFalse(self.pet.play_startup_animation(automatic=True))
        self.assertTrue(self.pet.play_startup_animation())
        self.assertFalse(self.pet._startup_splash.next_time.isChecked())
        self.pet._on_user_interaction()
        self.assertFalse(self.pet._startup_splash_active)

    def test_hidden_pet_does_not_open_or_reappear_after_animation(self):
        self.pet._desktop_hidden = True
        self.assertFalse(self.pet.play_startup_animation(automatic=True))
        self.pet._desktop_hidden = False
        self.assertTrue(self.pet.play_startup_animation())
        self.pet.hide()
        self.assertFalse(self.pet._startup_splash_active)
        self.assertFalse(self.pet._startup_splash.active)
        self.assertFalse(self.pet._welcome_timer.isActive())

    def test_animation_failure_leaves_regular_pet_and_greeting_available(self):
        self.pet._startup_splash = None
        self.pet._welcome_timer.start(1500)
        with patch('ui.startup_splash.GugaStartupSplash',side_effect=RuntimeError('missing art')):
            self.assertFalse(self.pet.play_startup_animation(automatic=True))
        self.assertFalse(self.pet._startup_splash_active)
        self.assertTrue(self.pet.isVisible())
        self.assertTrue(self.pet._welcome_timer.isActive())

    def test_replay_and_startup_switch_are_reachable_from_native_menu(self):
        menu = self.pet._ensure_pet_menu()
        more = next(a.menu() for a in menu.actions() if a.text() == '更多操作')
        appearance = next(a.menu() for a in more.actions() if a.text() == '桌宠外观')
        opening = next(a.menu() for a in appearance.actions() if a.text()=='开场动画')
        self.assertEqual([a.text() for a in opening.actions()],['重播开场动画','启动时播放开场动画','播放开场音效'])
        opening.actions()[1].setChecked(False)
        self.assertFalse(self.pet._config._s.value('startup/animation',True,type=bool))
        opening.actions()[0].trigger()
        self.assertTrue(self.pet._startup_splash_active)

    def test_automatic_handoff_enters_from_outside_and_preserves_landing_point(self):
        target = self.pet.pos()
        self.pet._welcome_timer.start(1500)
        self.assertTrue(self.pet.play_startup_animation(automatic=True))
        self.pet._startup_splash.finish()
        animation, landing, bounds = self.pet._startup_arrival
        self.assertEqual(landing, target)
        self.assertGreater(self.pet.x(), bounds.right())
        self.assertFalse(self.pet._welcome_timer.isActive())
        self.pet._save_state()
        self.assertEqual(self.pet._config.load_pos(), (target.x(), target.y()))
        animation.setCurrentTime(animation.duration() // 2)
        self.assertGreater(self.pet.x(), target.x())
        animation.setCurrentTime(animation.duration())
        self.assertIsNone(self.pet._startup_arrival)
        self.assertEqual(self.pet.pos(), target)
        self.assertTrue(self.pet._welcome_timer.isActive())


class EntryTests(unittest.TestCase):
    def test_normal_entry_requests_boot_animation_before_event_loop(self):
        import main
        with patch.object(main,'_single_instance',return_value=True), \
                patch.object(main,'QApplication') as app, patch.object(main,'PetWindow') as window:
            app.return_value.exec_.return_value = 0
            with self.assertRaises(SystemExit):main.main()
        window.return_value.play_startup_animation.assert_called_once_with(automatic=True)


if __name__ == '__main__':
    unittest.main()
