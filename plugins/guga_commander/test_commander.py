# -*- coding: utf-8 -*-
"""咕嘎指令台 · 单元测试（不需要 PyQt、不需要联网、不会真开软件或碰客户端窗口）。

    python test_commander.py
    python -m unittest discover -s plugins/guga_commander -p "test_*.py"
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

PLUGIN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PLUGIN_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.guga_commander import (app_lib, cloudmusic_lib, commander,  # noqa: E402
                                    consent, files_lib, host_hook, music_lib,
                                    startup, steam_lib, web_lib)


class _Ctx:
    """模仿 core.fast_commands._CommandText，只提供钩子用到的字段。"""

    def __init__(self, text):
        self.raw = text
        self.value = commander._clean(text)
        self.prefix = dict(source_text=text)


class ParseTests(unittest.TestCase):
    def test_open_variants(self):
        cases = {
            '打开微信': '微信',
            '把记事本打开吧': '记事本',
            'VS Code 开一下': 'Visual Studio Code',
            '帮我启动炉石传说': '炉石传说',
            '微信开一下': '微信',
        }
        for text, target in cases.items():
            with self.subTest(text=text):
                cmd = commander.parse(text)
                self.assertEqual(cmd.action, 'open_app')
                self.assertEqual(cmd.target, target)

    def test_auto_play_variants(self):
        cases = (
            '自己打一局战棋',
            '帮我玩炉石 30 步',
            '自动对战 5 分钟',
            '咕嘎你来打',
        )
        for text in cases:
            with self.subTest(text=text):
                cmd = commander.parse(text)
                self.assertEqual(cmd.action, 'auto_play')
                self.assertEqual(cmd.game, 'battleground')
                self.assertIn('停一下', cmd.note)

    def test_budget_is_clamped(self):
        cmd = commander.parse('自动对战 9999 步 9999 秒')
        self.assertEqual(cmd.max_actions, commander.MAX_ACTIONS_CEILING)
        self.assertEqual(cmd.seconds, commander.SECONDS_CEILING)
        cmd = commander.parse('自己玩炉石 30 步 5 分钟')
        self.assertEqual((cmd.max_actions, cmd.seconds), (30, 300))

    def test_no_auto_without_explicit_request(self):
        # 只说「打开游戏」绝不能偷偷启动自动接管。
        for text in ('打开炉石传说', '战棋怎么样', '帮我启动战网', '看看状态'):
            with self.subTest(text=text):
                self.assertNotEqual(commander.parse(text).action, 'auto_play')

    def test_stop_variants(self):
        for text in ('停一下', '别玩了', '住手', '停止自动', '不要打了', '别再自动打了'):
            with self.subTest(text=text):
                self.assertEqual(commander.parse(text).action, 'bg_stop')

    def test_other_game_is_honest(self):
        cmd = commander.parse('自己玩原神')
        self.assertEqual(cmd.action, 'unsupported_game')
        self.assertEqual(cmd.game, 'other')
        self.assertEqual(commander.plan_for(cmd), [])  # 不产生任何工具调用
        self.assertIn('只能帮你打开', cmd.note)

    def test_status_observe_step_list_help(self):
        cases = {
            '看看打到哪了': 'bg_status',
            '观战': 'bg_observe',
            '走一步': 'bg_step',
            '有哪些应用': 'list_apps',
            '怎么用': 'help',
        }
        for text, action in cases.items():
            with self.subTest(text=text):
                self.assertEqual(commander.parse(text).action, action)

    def test_unknown_text(self):
        cmd = commander.parse('今天天气怎么样')
        self.assertEqual(cmd.action, 'unknown')
        self.assertEqual(commander.plan_for(cmd), [])

    def test_compound_sentence_not_treated_as_launch(self):
        self.assertNotEqual(commander.parse('打开微信然后发消息').action, 'open_app')


class PlanTests(unittest.TestCase):
    HOST_TOOLS = {'desktop_command', 'battleground'}
    LOCAL_TOOLS = {'steam.open_client', 'steam.list_games', 'steam.rungame'}

    def test_all_tools_are_existing_host_tools(self):
        for text in ('打开微信', '自己打一局战棋', '走一步', '观战',
                     '看看打到哪了', '停一下', '有哪些应用', '打开 steam'):
            for tool, args, label in commander.plan_for(commander.parse(text)):
                self.assertIn(tool, self.HOST_TOOLS | self.LOCAL_TOOLS)
                self.assertIsInstance(args, dict)
                self.assertTrue(label)

    def test_steam_game_plan_carries_appid(self):
        cmd = commander.Command(action='open_steam_game', target='Counter-Strike 2', appid=730)
        plan = commander.plan_for(cmd)
        self.assertEqual(plan[0][0], 'steam.rungame')
        self.assertEqual(plan[0][1], dict(appid=730, name='Counter-Strike 2'))

    def test_auto_plan_budget(self):
        plan = commander.plan_for(commander.parse('自动对战 40 步 3 分钟'))
        self.assertEqual(plan[0][1], dict(action='auto', max_actions=40, seconds=180))

    def test_open_plan_target(self):
        plan = commander.plan_for(commander.parse('把记事本打开'))
        self.assertEqual(plan[0][1], dict(action='open', target='记事本'))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake_call(self, tool, args):
        self.calls.append((tool, dict(args)))
        return dict(ok=True, message='宿主回执')

    def test_run_success(self):
        cmd = commander.parse('自己打一局战棋')
        result = commander.run(cmd, self.fake_call)
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], 'battleground')
        self.assertEqual(result['receipts'][0]['tool'], 'battleground')
        self.assertIn('停一下', result['message'])

    def test_run_failure_stops_immediately(self):
        def failing(tool, args):
            self.calls.append((tool, dict(args)))
            return dict(ok=False, error='战棋任务尚在运行；先查询状态或停止')

        result = commander.run(commander.parse('自己打一局战棋'), failing)
        self.assertFalse(result['ok'])
        self.assertIn('战棋任务尚在运行', result['message'])
        self.assertEqual(len(result['receipts']), 1)

    def test_battleground_reply_without_ok_is_not_a_failure(self):
        # BattlegroundDirector.start() 返回 accepted/job_id，没有 ok 字段。
        def accepted(tool, args):
            return dict(accepted=True, job_id='abc', mode='auto')

        result = commander.run(commander.parse('自己打一局战棋'), accepted)
        self.assertTrue(result['ok'], '不能把宿主的正常启动回执误判成失败')

    def test_help_and_unsupported_do_not_call_host(self):
        for text in ('怎么用', '自己玩原神', '今天天气怎么样'):
            commander.run(commander.parse(text), self.fake_call)
        self.assertEqual(self.calls, [])

    def test_hook_execute_uses_injected_call(self):
        # monkeypatch 默认执行器，验证 hook -> commander.run 的完整链路。
        original = commander.default_call
        commander.default_call = self.fake_call
        try:
            matched = host_hook.match_rule(_Ctx('停一下'))
            self.assertEqual(matched['action'], 'guga_commander')
            answer = host_hook.execute(matched)
            self.assertTrue(answer['ok'])
            self.assertEqual(self.calls[0], ('battleground', dict(action='stop')))
        finally:
            commander.default_call = original

    def test_steam_game_runs_locally_without_host(self):
        cmd = commander.Command(action='open_steam_game',
                                target='Red Dead Redemption 2', appid=1174180)
        with mock.patch.object(commander.steam_lib, 'launch_game',
                               return_value=dict(ok=True,
                                                 message='已请求 Steam 启动《Red Dead Redemption 2》。')) as launch:
            result = commander.run(cmd, self.fake_call)
        self.assertTrue(result['ok'])
        launch.assert_called_once_with(1174180, 'Red Dead Redemption 2')
        self.assertEqual(self.calls, [], 'Steam 动作不该绕去宿主 runtime')
        self.assertIn('Steam', result['message'])

    def test_missing_steam_game_lists_installed(self):
        cmd = commander.Command(action='open_steam_game', target='黑神话悟空', appid=0)
        games = [dict(appid=730, name='Counter-Strike 2', ready=True)]
        with mock.patch.object(commander, '_steam_lookup', return_value=None), \
                mock.patch.object(commander.steam_lib, 'installed_games', return_value=games):
            result = commander.run(cmd, self.fake_call)
        self.assertFalse(result['ok'])
        self.assertIn('没找到', result['message'])
        self.assertIn('Counter-Strike 2', result['message'])
        self.assertEqual(self.calls, [])

    def test_steam_list_runs_locally(self):
        cmd = commander.Command(action='list_steam_games')
        games = [dict(appid=730, name='Counter-Strike 2', ready=True)]
        with mock.patch.object(commander.steam_lib, 'installed_games', return_value=games):
            result = commander.run(cmd, self.fake_call)
        self.assertTrue(result['ok'])
        self.assertIn('1', result['message'])
        self.assertEqual(self.calls, [])


class HookTests(unittest.TestCase):
    def test_unknown_returns_none(self):
        self.assertIsNone(host_hook.match_rule(_Ctx('今天天气怎么样')))

    def test_open_is_handed_back_to_native_pipeline(self):
        result = host_hook.match_rule(_Ctx('把记事本打开吧'))
        self.assertEqual(result['action'], 'open_app')
        self.assertEqual(result['kind'], 'app')
        self.assertEqual(result['target'], '记事本')

    def test_auto_goes_to_plugin(self):
        result = host_hook.match_rule(_Ctx('自己打一局战棋'))
        self.assertEqual(result['action'], 'guga_commander')
        self.assertEqual(result['guga_command']['game'], 'battleground')

    def test_hook_never_raises_on_broken_ctx(self):
        self.assertIsNone(host_hook.match_rule(object()))

    def test_steam_goes_to_plugin(self):
        result = host_hook.match_rule(_Ctx('打开 steam'))
        self.assertEqual(result['action'], 'guga_commander')
        self.assertEqual(result['guga_command']['action'], 'open_steam')

    def test_unknown_target_is_not_stolen(self):
        # 「打开京东」既不是 Steam 游戏也不是已知软件：必须交回原生/智能体，
        # 插在 _match_open_app 之前也不能抢它的路由。
        with mock.patch.object(commander, '_steam_lookup', return_value=None):
            self.assertIsNone(host_hook.match_rule(_Ctx('打开京东')))

    def test_installed_app_not_in_native_list_opens_locally(self):
        # 「打开网易云音乐」：原生把它当网址丢给模型（DNS 报错就是这么来的），
        # 插件必须自己接住并直接启动。
        hit = dict(ok=True, path=r'C:\x\cloudmusic.exe', name='cloudmusic',
                   kind='exe', candidates=[])
        with mock.patch.object(host_hook, '_host_handles', return_value=False), \
                mock.patch.object(host_hook, '_site_alias', return_value=False), \
                mock.patch.object(host_hook, '_installed_app', return_value=hit):
            result = host_hook.match_rule(_Ctx('打开网易云音乐'))
        self.assertEqual(result['action'], 'guga_commander')
        self.assertEqual(result['guga_command']['action'], 'open_local_app')
        self.assertEqual(result['guga_command']['target'], '网易云音乐')

    def test_missing_app_is_not_claimed(self):
        # 本机没装：宁可放行让智能体如实说明，也不能假装能开。
        with mock.patch.object(host_hook, '_host_handles', return_value=False), \
                mock.patch.object(host_hook, '_site_alias', return_value=False), \
                mock.patch.object(host_hook, '_installed_app', return_value=None):
            self.assertIsNone(host_hook.match_rule(_Ctx('打开某个没装的软件')))

    def test_website_alias_is_never_launched_as_app(self):
        hit = dict(ok=True, path=r'C:\x\京东.exe', name='京东', kind='exe', candidates=[])
        with mock.patch.object(host_hook, '_installed_app', return_value=hit):
            self.assertIsNone(host_hook.match_rule(_Ctx('打开京东')))


class AppLibTests(unittest.TestCase):
    """本地软件解析：用注入的假清单，不依赖本机装了什么，也不会真启动。"""

    ITEMS = (
        dict(name='网易云音乐', path=r'C:\Start\网易云音乐.lnk', kind='lnk',
             norm=app_lib._norm('网易云音乐')),
        dict(name='cloudmusic', path=r'D:\games\cloudmusic.exe', kind='exe',
             norm=app_lib._norm('cloudmusic')),
        dict(name='云音乐', path=r'C:\Start\云音乐.lnk', kind='lnk',
             norm=app_lib._norm('云音乐')),
        dict(name='酷音乐', path=r'C:\Start\酷音乐.lnk', kind='lnk',
             norm=app_lib._norm('酷音乐')),
        dict(name='Visual Studio Code', path=r'C:\Start\Code.lnk', kind='lnk',
             norm=app_lib._norm('Visual Studio Code')),
    )

    def test_exact_name_wins(self):
        hit = app_lib.resolve('Visual Studio Code', self.ITEMS)
        self.assertTrue(hit['ok'])
        self.assertEqual(hit['name'], 'Visual Studio Code')

    def test_alias_hint_maps_chinese_name_to_exe(self):
        # 中文名和 exe 名完全不像：靠 EXE_HINTS 对上
        hit = app_lib.resolve('网易云音乐', self.ITEMS)
        self.assertTrue(hit['ok'])
        self.assertEqual(hit['path'], r'D:\games\cloudmusic.exe')

    def test_partial_name_is_enough(self):
        hit = app_lib.resolve('网易云', self.ITEMS)
        self.assertTrue(hit['ok'])
        self.assertIn('网易云', hit['name'])

    def test_tie_returns_candidates_instead_of_guessing(self):
        hit = app_lib.resolve('音乐', self.ITEMS)
        self.assertFalse(hit['ok'])
        self.assertGreaterEqual(len(hit['candidates']), 2)

    def test_unknown_returns_quietly(self):
        hit = app_lib.resolve('完全不存在的软件名', self.ITEMS)
        self.assertFalse(hit['ok'])
        self.assertEqual(hit['candidates'], [])
        self.assertEqual(hit['error'], '')

    def test_launch_asks_windows_to_start_it(self):
        with mock.patch.object(app_lib.os, 'startfile') as startfile:
            result = app_lib.launch('Visual Studio Code', self.ITEMS)
        self.assertTrue(result['ok'])
        startfile.assert_called_once_with(r'C:\Start\Code.lnk')

    def test_launch_reports_ambiguous(self):
        with mock.patch.object(app_lib.os, 'startfile') as startfile:
            result = app_lib.launch('音乐', self.ITEMS)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'ambiguous')
        startfile.assert_not_called()

    def test_uninstall_shortcuts_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '网易云音乐.lnk').write_bytes(b'')
            (root / '卸载网易云音乐.lnk').write_bytes(b'')
            found = [Path(p).name for p in app_lib._walk_shortcuts(str(root))]
        self.assertIn('网易云音乐.lnk', found)
        self.assertNotIn('卸载网易云音乐.lnk', found)

    def test_empty_name_is_not_a_wildcard(self):
        self.assertFalse(app_lib.resolve('', self.ITEMS)['ok'])
        self.assertFalse(app_lib.resolve('   ', self.ITEMS)['ok'])


class LocalAppRunTests(unittest.TestCase):
    def test_open_local_app_uses_app_lib(self):
        with mock.patch.object(commander.app_lib, 'launch',
                               return_value=dict(ok=True, message='已请 Windows 启动')) as launch:
            result = commander.run(commander.Command(action='open_local_app',
                                                     target='网易云音乐'))
        self.assertTrue(result['ok'])
        launch.assert_called_once_with('网易云音乐')
        self.assertEqual(result['receipts'][0]['tool'], 'local.open_app')

    def test_play_anything_else_is_still_refused(self):
        cmd = commander.parse('自己玩原神')
        self.assertEqual(cmd.action, 'unsupported_game')


class SteamMatchTests(unittest.TestCase):
    GAMES = (
        dict(appid=1174180, name='Red Dead Redemption 2', ready=True),
        dict(appid=292030, name='The Witcher 3: Wild Hunt — Remastered', ready=True),
        dict(appid=29310, name='The Witcher 2: Assassins of Kings', ready=True),
        dict(appid=730, name='Counter-Strike 2', ready=True),
        dict(appid=275850, name="No Man's Sky", ready=False),
    )

    def test_chinese_nicknames(self):
        cases = {'大表哥2': 1174180, '荒野大镖客': 1174180, '巫师3': 292030,
                 'cs2': 730, '无人深空': 275850}
        for query, appid in cases.items():
            with self.subTest(query=query):
                hit = steam_lib.match(query, self.GAMES)
                self.assertIsNotNone(hit, query)
                self.assertEqual(hit['appid'], appid)

    def test_number_keeps_sequels_apart(self):
        self.assertEqual(steam_lib.match('巫师2', self.GAMES)['appid'], 29310)
        self.assertEqual(steam_lib.match('巫师3', self.GAMES)['appid'], 292030)

    def test_english_name_exact(self):
        self.assertEqual(steam_lib.match('Red Dead Redemption 2', self.GAMES)['appid'],
                         1174180)

    def test_unknown_and_blank_return_none(self):
        self.assertIsNone(steam_lib.match('黑神话悟空', self.GAMES))
        self.assertIsNone(steam_lib.match('   ', self.GAMES))

    def test_ambiguous_returns_candidates(self):
        games = (dict(appid=1, name='Alpha Game', ready=True),
                 dict(appid=2, name='Beta Game', ready=True))
        hit = steam_lib.match('game', games)
        self.assertIsNotNone(hit)
        self.assertEqual(len(hit['candidates']), 2)

    def test_normalize(self):
        self.assertEqual(steam_lib.normalize('The Witcher 3: Wild Hunt — Remastered'),
                         'thewitcher3wildhuntremastered')


class SteamParseTests(unittest.TestCase):
    HIT = dict(appid=292030, name='The Witcher 3: Wild Hunt — Remastered', candidates=[])

    def test_open_client(self):
        for text in ('打开 steam', '启动 Steam', 'steam'):
            with self.subTest(text=text):
                self.assertEqual(commander.parse(text).action, 'open_steam')

    def test_list_games(self):
        for text in ('我 steam 里有什么游戏', 'steam 上有哪些游戏', '我的游戏库'):
            with self.subTest(text=text):
                self.assertEqual(commander.parse(text).action, 'list_steam_games')

    def test_game_by_explicit_words(self):
        with mock.patch.object(commander, '_steam_lookup', return_value=self.HIT):
            for text in ('打开 steam 上的巫师3', '在 steam 里玩巫师3',
                         '打开游戏 巫师3', '玩一下巫师3', '把巫师3打开'):
                with self.subTest(text=text):
                    cmd = commander.parse(text)
                    self.assertEqual(cmd.action, 'open_steam_game')
                    self.assertEqual(cmd.appid, 292030)

    def test_bare_game_name(self):
        hit = dict(appid=1174180, name='Red Dead Redemption 2', candidates=[])
        with mock.patch.object(commander, '_steam_lookup', return_value=hit):
            self.assertEqual(commander.parse('打开大表哥2').action, 'open_steam_game')

    def test_missing_game_is_reported_honestly(self):
        with mock.patch.object(commander, '_steam_lookup', return_value=None):
            cmd = commander.parse('打开 steam 上的黑神话悟空')
        self.assertEqual(cmd.action, 'open_steam_game')
        self.assertEqual(cmd.appid, 0)
        self.assertIn('没找到', cmd.note)

    def test_ambiguous_game_asks_for_full_name(self):
        hit = dict(appid=0, name='', candidates=[dict(appid=1, name='A'),
                                                 dict(appid=2, name='B')])
        with mock.patch.object(commander, '_steam_lookup', return_value=hit):
            cmd = commander.parse('打开游戏 星球')
        self.assertEqual(cmd.action, 'open_steam_game')
        self.assertEqual(cmd.appid, 0)
        self.assertIn('多个', cmd.note)

    def test_hearthstone_stays_with_native_route(self):
        # 即使 Steam 库里恰好有个同名条目，炉石也不该被 Steam 抢走
        hit = dict(appid=999, name='Hearthstone', candidates=[])
        with mock.patch.object(commander, '_steam_lookup', return_value=hit):
            self.assertEqual(commander.parse('打开炉石传说').action, 'open_app')
            self.assertEqual(commander.parse('玩一下炉石').action, 'open_app')
            self.assertNotEqual(commander.parse('玩一下炉石').action, 'open_steam_game')

    def test_open_without_game_name_asks(self):
        self.assertEqual(commander.parse('打开游戏').action, 'help')


class CardTests(unittest.TestCase):
    def test_market_card_matches_project_schema(self):
        card = commander.market_card()
        self.assertEqual(card['schema'], 1)
        self.assertEqual(card['id'], 'guga-commander')
        self.assertEqual(card['id'], PLUGIN_DIR.name.replace('_', '-'))
        self.assertRegex(card['id'], r'^[a-z][a-z0-9-]{1,62}$')
        self.assertRegex(card['version'], r'^\d+\.\d+\.\d+$')
        self.assertIn(card['kind'], ('workflow', 'codex-skill', 'feature'))
        self.assertTrue(card['name'] and len(card['name']) <= 50)
        self.assertTrue(card['description'] and len(card['description']) <= 280)
        self.assertTrue(1 <= len(card['body']) <= 7200)

    def test_installed_card_is_valid_json(self):
        path = PROJECT_ROOT / 'plugins' / 'community' / 'guga-commander.json'
        if not path.is_file():
            self.skipTest('插件尚未安装')
        data = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(data['id'], 'guga-commander')


class InstallerRoundTripTests(unittest.TestCase):
    """在临时副本上演练安装/卸载，绝不碰用户真实的核心文件。"""

    def test_hook_block_compiles_and_restores_bytes(self):
        spec = importlib.util.spec_from_file_location(
            'guga_installer_under_test', PLUGIN_DIR / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)

        real_source = (PROJECT_ROOT / 'core' / 'fast_commands.py').read_bytes()
        # 演练用一份不含钩子的干净副本
        clean = real_source.partition(installer.HOOK_BEGIN)[0].rstrip(b'\r\n') + b'\n'

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'core').mkdir()
            (root / 'plugins' / 'community').mkdir(parents=True)
            target = root / 'core' / 'fast_commands.py'
            target.write_bytes(clean)
            # main.py 也要放进隔离目录：否则安装器会去改真实的 main.py
            main_py = root / 'main.py'
            main_clean = (b'import sys\n\ndef main():\n'
                          b'    print("fake")\n'
                          b'    sys.exit(app.exec_())\n\nmain()\n')
            main_py.write_bytes(main_clean)

            installer.FAST_COMMANDS = target
            installer.BACKUP = root / 'core' / 'fast_commands.py.guga-commander.bak'
            installer.CARD = root / 'plugins' / 'community' / 'guga-commander.json'
            installer.PROJECT_ROOT = root

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(installer.install(), 0)
            patched = target.read_bytes()
            compile(patched, str(target), 'exec')  # 装完必须仍是合法 Python
            self.assertIn(installer.HOOK_BEGIN, patched)
            self.assertTrue(installer.CARD.is_file())
            # 启动授权弹窗也必须只落在临时 main.py 里
            main_patched = main_py.read_bytes()
            compile(main_patched, str(main_py), 'exec')
            self.assertIn(installer.STARTUP_BEGIN, main_patched)

            # 重复安装必须幂等
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(installer.install(), 0)
            self.assertEqual(target.read_bytes(), patched)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(installer.uninstall(), 0)
            self.assertEqual(target.read_bytes(), clean, '卸载必须字节级还原')
            self.assertEqual(main_py.read_bytes(), main_clean, 'main.py 也必须字节级还原')
            self.assertFalse(installer.CARD.exists())
            self.assertFalse(installer._main_backup().exists(), '备份文件要清理干净')

    def test_hook_markers_are_paired(self):
        text = (PLUGIN_DIR / 'install.py').read_text(encoding='utf-8')
        self.assertIn('# --- guga-commander plugin hook', text)
        self.assertIn('# --- end guga-commander plugin hook ---', text)


class FileParseTests(unittest.TestCase):
    def test_paths_go_to_file_route(self):
        for text in ('打开 D:\\报告.pdf', '把桌面上的合同.docx打开',
                     '打开我的文档', '看看桌面上的照片.png',
                     '打开下载里的安装包.zip'):
            with self.subTest(text=text):
                self.assertEqual(commander.parse(text).action, 'open_file')

    def test_open_file_without_name_asks(self):
        self.assertEqual(commander.parse('打开文件').action, 'help')

    def test_permission_commands(self):
        self.assertEqual(commander.parse('允许读取文件').action, 'file_access_on')
        self.assertEqual(commander.parse('打开本机文件').action, 'file_access_on')
        self.assertEqual(commander.parse('关闭文件权限').action, 'file_access_off')
        self.assertEqual(commander.parse('取消文件访问').action, 'file_access_off')

    def test_apps_and_games_are_not_files(self):
        for text in ('打开微信', '打开 steam', '打开大表哥2', '打开音乐'):
            with self.subTest(text=text):
                self.assertNotEqual(commander.parse(text).action, 'open_file')

    def test_plan_uses_local_tool(self):
        plan = commander.plan_for(commander.Command(action='open_file', target='a.pdf'))
        self.assertEqual(plan[0][0], 'local.open_file')
        plan = commander.plan_for(commander.Command(action='file_access_on'))
        self.assertEqual(plan[0][0], 'local.file_access')
        self.assertTrue(plan[0][1]['granted'])


class ConsentTests(unittest.TestCase):
    class _Stub:
        def __init__(self, enabled=True):
            self.enabled = enabled
            self.calls = []

        def set_enabled(self, value):
            self.calls.append(value)
            self.enabled = bool(value)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.stub = self._Stub(True)
        for name, value in (('STATE', self.dir / 'file_access.json'),
                            ('CORE_STATE', self.dir / 'missing.json'),
                            ('_access', lambda *a, **k: self.stub)):
            patcher = mock.patch.object(consent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_never_asked_means_no_permission(self):
        # 严格默认：没问过就不许读文件，即使原生开关默认是开的。
        self.assertFalse(consent.is_granted())

    def test_grant_then_revoke(self):
        consent.set_granted(True, 'startup')
        self.assertTrue(consent.is_granted())
        self.assertEqual(self.stub.calls, [True])
        consent.set_granted(False, 'startup')
        self.assertFalse(consent.is_granted())
        self.assertEqual(self.stub.calls, [True, False])

    def test_explicit_deny_survives_switch_flip(self):
        consent.set_granted(False, 'startup')
        self.stub.enabled = True  # 有人事后把完整访问又打开
        self.assertFalse(consent.is_granted(), '明确拒绝过就要重新同意')

    def test_panel_switch_counts_as_consent(self):
        panel = self.dir / 'local_access.json'
        panel.write_text('{"full_access": true}', encoding='utf-8')
        with mock.patch.object(consent, 'CORE_STATE', panel):
            self.assertTrue(consent.is_granted())

    def test_panel_after_deny_counts_as_new_consent(self):
        # 弹窗告诉用户「想改就去软件权限里勾」——那条路必须真的能用。
        self.stub.enabled = False
        consent.set_granted(False, 'startup')
        state = self.dir / 'file_access.json'
        data = json.loads(state.read_text(encoding='utf-8'))
        data['denied_at'] = '2020-01-01T00:00:00.000'  # 把拒绝时间挪到很久以前
        state.write_text(json.dumps(data), encoding='utf-8')
        panel = self.dir / 'local_access.json'
        panel.write_text('{"full_access": true}', encoding='utf-8')
        with mock.patch.object(consent, 'CORE_STATE', panel):
            self.stub.enabled = True
            self.assertTrue(consent.is_granted())

    def test_stale_panel_file_does_not_override_deny(self):
        panel = self.dir / 'local_access.json'
        panel.write_text('{"full_access": true}', encoding='utf-8')
        with mock.patch.object(consent, 'CORE_STATE', panel):
            consent.set_granted(False, 'startup')
            self.stub.enabled = True  # 开关被别的东西打开，但用户没再动过面板
            self.assertFalse(consent.is_granted(), '旧面板状态不算新的同意')

    def test_status_reports_both_sides(self):
        consent.set_granted(True, 'startup')
        data = consent.status()
        self.assertTrue(data['granted'])
        self.assertTrue(data['core_full_access'])
        self.assertTrue(data['granted_at'])

    # —— 只问一次 ——
    def test_asks_only_before_the_first_choice(self):
        self.assertTrue(consent.should_ask(), '第一次启动应当问一次')
        consent.set_granted(True, 'startup')
        consent.mark_asked('allow')
        self.assertFalse(consent.should_ask(), '允许过就不再打扰')
        self.assertFalse(consent.should_ask())

    def test_deny_is_remembered_and_not_repeated(self):
        consent.set_granted(False, 'startup')
        consent.mark_asked('deny')
        self.assertFalse(consent.should_ask(), '拒绝也是答案，不该每次再问')
        self.assertFalse(consent.is_granted())

    def test_panel_switch_suppresses_the_prompt(self):
        panel = self.dir / 'local_access.json'
        panel.write_text('{"full_access": true}', encoding='utf-8')
        with mock.patch.object(consent, 'CORE_STATE', panel):
            self.assertFalse(consent.should_ask(),
                             '用户自己在软件权限里开过就不用再问')

    def test_asked_once_means_silent_startup(self):
        consent.set_granted(True, 'startup')
        consent.mark_asked('allow')
        with mock.patch.object(startup, '_build_dialog',
                               side_effect=AssertionError('不该再弹窗')):
            self.assertIsNone(startup.ask_now(None))

    def test_hint_points_at_the_real_menu(self):
        # 提示语必须指向真实入口，否则「想允许时从哪里打开」就成了空话。
        self.assertIn('软件权限', consent.NO_PERMISSION_HINT)
        self.assertIn('···', consent.NO_PERMISSION_HINT)


class FilesLibTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('报告.pdf', '合同.docx', '照片.png'):
            (self.root / name).write_bytes(b'x')
        self.roots = [self.root]

    def test_absolute_path(self):
        hit = files_lib.resolve(str(self.root / '报告.pdf'))
        self.assertTrue(hit['ok'])
        self.assertEqual(hit['kind'], 'file')

    def test_folder_is_recognised(self):
        hit = files_lib.resolve(str(self.root))
        self.assertTrue(hit['ok'])
        self.assertEqual(hit['kind'], 'folder')

    def test_search_by_bare_name(self):
        hit = files_lib.resolve('合同.docx', self.roots)
        self.assertTrue(hit['ok'])
        self.assertEqual(Path(hit['path']).name, '合同.docx')

    def test_missing_file_is_not_guessed(self):
        hit = files_lib.resolve(str(self.root / '没有这个.pdf'))
        self.assertFalse(hit['ok'])
        self.assertEqual(hit['candidates'], [])

    def test_duplicate_names_ask_instead_of_picking(self):
        backup = self.root / '备份'
        backup.mkdir()
        (backup / '合同.docx').write_bytes(b'x')
        hit = files_lib.resolve('合同.docx', self.roots)
        self.assertFalse(hit['ok'])
        self.assertGreaterEqual(len(hit['candidates']), 2)

    def test_looks_like_path(self):
        for text in ('D:\\a.pdf', 'C:/Users/000/a.txt', '~/x.txt', '报告.pdf',
                     '桌面上的合同.docx', '我的文档'):
            with self.subTest(text=text):
                self.assertTrue(files_lib.looks_like_path(text))
        for text in ('微信', '京东', 'b站', '原神', 'steam'):
            with self.subTest(text=text):
                self.assertFalse(files_lib.looks_like_path(text))

    def test_open_without_consent_reads_nothing(self):
        with mock.patch.object(consent, 'is_granted', return_value=False), \
                mock.patch.object(files_lib.os, 'startfile') as startfile:
            result = files_lib.open_target('报告.pdf', self.roots)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'no_consent')
        startfile.assert_not_called()
        self.assertIn('允许', result['error'])

    def test_open_with_consent(self):
        with mock.patch.object(consent, 'is_granted', return_value=True), \
                mock.patch.object(files_lib.os, 'startfile') as startfile:
            result = files_lib.open_target('报告.pdf', self.roots)
        self.assertTrue(result['ok'])
        startfile.assert_called_once()
        self.assertEqual(Path(startfile.call_args[0][0]).name, '报告.pdf')

    def test_open_failure_is_reported(self):
        with mock.patch.object(consent, 'is_granted', return_value=True), \
                mock.patch.object(files_lib.os, 'startfile', side_effect=OSError('拒绝访问')):
            result = files_lib.open_target('报告.pdf', self.roots)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'open_failed')


class StartupTests(unittest.TestCase):
    def test_without_gui_it_does_not_block_or_crash(self):
        # 测试环境没有 QApplication：应当安静返回 None，绝不弹窗、绝不改权限。
        self.assertIsNone(startup.ask_at_startup(None))

    def test_permission_can_be_granted_by_command(self):
        with mock.patch.object(consent, 'set_granted',
                               return_value=dict(granted=True)) as set_granted:
            result = commander.run(commander.Command(action='file_access_on'))
        self.assertTrue(result['ok'])
        set_granted.assert_called_once_with(True, source='command')


class WebLibTests(unittest.TestCase):
    def test_aliases_resolve_to_one_site(self):
        for name in ('B站', 'b站', '哔哩哔哩', 'bilibili', '小破站', 'B站官网'):
            self.assertEqual(web_lib.resolve(name)['name'], 'B站', name)

    def test_unknown_name_is_not_guessed(self):
        self.assertIsNone(web_lib.resolve('我家院子'))
        self.assertIsNone(web_lib.resolve(''))

    def test_search_url_encodes_the_keyword(self):
        url = web_lib.search_url('B站', '猫 猫')
        self.assertEqual(url,
                         'https://search.bilibili.com/all?keyword=%E7%8C%AB%20%E7%8C%AB')

    def test_search_url_needs_a_keyword(self):
        self.assertEqual(web_lib.search_url('B站', '   '), '')
        self.assertEqual(web_lib.search_url('不存在的站', '猫'), '')

    def test_music_platforms_are_marked(self):
        self.assertTrue(web_lib.resolve('网易云')['music'])
        self.assertTrue(web_lib.resolve('QQ音乐')['music'])
        self.assertFalse(web_lib.resolve('知乎')['music'])

    def test_engine_defaults_to_bing(self):
        self.assertIn('bing.com/search', web_lib.engine_url('今天天气'))

    def test_domain_detection(self):
        self.assertTrue(web_lib.is_domain('example.com'))
        self.assertTrue(web_lib.is_domain('https://a.cn/x?y=1'))
        self.assertFalse(web_lib.is_domain('京东'))
        self.assertFalse(web_lib.is_domain('有 空格.com'))

    def test_open_url_reports_failure_instead_of_raising(self):
        with mock.patch.object(web_lib.os, 'startfile',
                               side_effect=OSError('没有默认浏览器'), create=True):
            result = web_lib.open_url('https://example.com')
        self.assertFalse(result['ok'])
        self.assertIn('没有默认浏览器', result['error'])

    def test_unknown_site_name_falls_back_to_search(self):
        opened = []
        with mock.patch.object(web_lib, 'open_url',
                               side_effect=lambda url: opened.append(url) or dict(ok=True)):
            result = web_lib.open_site('天马行空的个人站')
        self.assertTrue(result['ok'])
        self.assertIn('bing.com/search', opened[0])
        self.assertTrue(result.get('searched'))

    def test_open_site_without_name_opens_the_browser(self):
        opened = []
        with mock.patch.object(web_lib, 'open_url',
                               side_effect=lambda url: opened.append(url) or dict(ok=True)):
            web_lib.open_site('')
        self.assertEqual(opened, ['https://www.bing.com'])

    def test_known_site_opens_its_homepage(self):
        opened = []
        with mock.patch.object(web_lib, 'open_url',
                               side_effect=lambda url: opened.append(url) or dict(ok=True)):
            result = web_lib.open_site('小破站官网')
        self.assertEqual(opened, ['https://www.bilibili.com'])
        self.assertEqual(result['site'], 'B站')


class WebParseTests(unittest.TestCase):
    def test_site_search(self):
        cmd = commander.parse('在B站搜索猫')
        self.assertEqual((cmd.action, cmd.site, cmd.target), ('site_search', 'B站', '猫'))

    def test_prefix_and_suffix_variants(self):
        cases = (('用必应查天气', '必应', '天气'),
                 ('淘宝上搜机械键盘', '淘宝', '机械键盘'),
                 ('帮我在知乎搜一下 PyQt', '知乎', 'PyQt'),
                 ('B站搜原神', 'B站', '原神'))
        for text, site, query in cases:
            cmd = commander.parse(text)
            self.assertEqual((cmd.action, cmd.site, cmd.target),
                             ('site_search', site, query), text)

    def test_music_platform_search(self):
        for text in ('打开网易云音乐搜索晴天', '网易云音乐搜晴天'):
            cmd = commander.parse(text)
            self.assertEqual((cmd.action, cmd.target), ('music_search', '晴天'), text)

    def test_song_phrase_searches_music(self):
        cmd = commander.parse('搜一首周杰伦的歌')
        self.assertEqual((cmd.action, cmd.target), ('music_search', '周杰伦'))

    def test_like_song_phrases(self):
        cases = (('把晴天加入我喜欢', '晴天'),
                 ('把晴天加进我喜欢的音乐', '晴天'),
                 ('收藏周杰伦的晴天', '周杰伦的晴天'))
        for text, song in cases:
            cmd = commander.parse(text)
            self.assertEqual((cmd.action, cmd.target), ('music_like', song), text)

    def test_like_without_a_song_asks_which_one(self):
        cmd = commander.parse('帮我添加一首音乐进我的喜欢中')
        self.assertEqual(cmd.action, 'music_like')
        self.assertEqual(cmd.target, '')
        self.assertIn('哪一首', cmd.note)

    def test_this_song_is_not_a_title(self):
        self.assertEqual(commander.parse('把这首歌加进我喜欢').target, '')

    def test_open_site_variants(self):
        self.assertEqual(commander.parse('打开一个网站').action, 'open_site')
        self.assertEqual(commander.parse('打开一个网站').target, '')
        self.assertEqual(commander.parse('打开小破站网站').target, '小破站')
        self.assertEqual(commander.parse('打开一个小网站').target, '')

    def test_chatting_is_not_a_song(self):
        self.assertEqual(commander.parse('喜欢吗').action, 'unknown')

    def test_verb_fragment_is_not_a_search(self):
        self.assertEqual(commander.parse('在知乎搜索').action, 'unknown')


class WebRunTests(unittest.TestCase):
    def test_site_search_runs_locally_without_the_host(self):
        calls = []

        def fake(site, query):
            calls.append((site, query))
            return dict(ok=True, message='已在%s搜索「%s」。' % (site, query))

        with mock.patch.object(web_lib, 'open_search', side_effect=fake):
            result = commander.run(commander.parse('在B站搜索猫'),
                                   call=lambda *a: self.fail('不该惊动宿主'))
        self.assertTrue(result['ok'])
        self.assertEqual(calls, [('B站', '猫')])
        self.assertEqual(result['message'], '已在B站搜索「猫」。')

    def test_music_like_failure_keeps_the_original_reason(self):
        # 客户端认不出界面 → 退回网页版；网页版的失败原因要原样说出来。
        with mock.patch.object(cloudmusic_lib, 'like',
                               return_value=dict(ok=False, code='ui_not_found',
                                                 error='没在客户端里认出搜索结果行')), \
             mock.patch.object(music_lib, 'pick', return_value=dict(ok=False)), \
             mock.patch.object(music_lib, 'like_song',
                               return_value=dict(ok=False, error='需要先开启「完整访问」')):
            result = commander.run(commander.parse('把晴天加入我喜欢'))
        self.assertFalse(result['ok'])
        self.assertIn('需要先开启「完整访问」', result['message'])
        self.assertIn('客户端那边', result['message'])

    def test_music_like_stays_in_the_client_when_it_works(self):
        with mock.patch.object(cloudmusic_lib, 'like',
                               return_value=dict(ok=True, verified=True,
                                                 message='已把《晴天》加进「我喜欢的音乐」。')), \
             mock.patch.object(music_lib, 'pick', return_value=dict(ok=False)), \
             mock.patch.object(music_lib, 'like_song',
                               side_effect=AssertionError('客户端成功时不该退回网页')):
            result = commander.run(commander.parse('把晴天加入我喜欢'))
        self.assertTrue(result['ok'])
        self.assertIn('我喜欢的音乐', result['message'])

    def test_unverified_like_does_not_fall_back_to_the_browser(self):
        # 已经点过 ♥ 却没确认到：再在浏览器点一次会重复动作，必须停下来说清楚。
        with mock.patch.object(cloudmusic_lib, 'like',
                               return_value=dict(ok=False, code='unverified',
                                                 error='点了 ♥，但没确认到它变红')), \
             mock.patch.object(music_lib, 'pick', return_value=dict(ok=False)), \
             mock.patch.object(music_lib, 'like_song',
                               side_effect=AssertionError('不确定时不该再点第二次')):
            result = commander.run(commander.parse('把晴天加入我喜欢'))
        self.assertFalse(result['ok'])
        self.assertIn('没确认到', result['message'])

    def test_music_search_prefers_the_client(self):
        with mock.patch.object(cloudmusic_lib, 'search',
                               return_value=dict(ok=True, message='已在网易云音乐里搜「晴天」。')), \
             mock.patch.object(music_lib, 'open_search',
                               side_effect=AssertionError('客户端能用就别开网页')):
            result = commander.run(commander.parse('网易云音乐搜晴天'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['message'], '已在网易云音乐里搜「晴天」。')

    def test_music_search_falls_back_to_the_browser(self):
        with mock.patch.object(cloudmusic_lib, 'search',
                               return_value=dict(ok=False, code='no_client',
                                                 error='没找到客户端')), \
             mock.patch.object(music_lib, 'open_search',
                               return_value=dict(ok=True, message='已在网易云音乐搜「晴天」。')), \
             mock.patch.object(music_lib, 'pick', return_value=dict(ok=False)):
            result = commander.run(commander.parse('网易云音乐搜晴天'))
        self.assertTrue(result['ok'])
        self.assertIn('网页版', result['message'])


_SONG = dict(id=186016, name='晴天', artists='周杰伦', album='', duration=0)


class _FakeSession:
    """假浏览器：按顺序吐出快照，记下点击参数。"""

    def __init__(self, snapshots, click_ok=True):
        self.snapshots = list(snapshots)
        self.click_ok = click_ok
        self.clicks = []
        self.stopped = False

    def navigate(self, url, cancel):
        return dict(ok=True, url=url, message='已在原来的浏览器打开这个网址。')

    def snapshot(self, cancel):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def execute(self, args, cancel):
        if args.get('action') == 'session_stop':
            self.stopped = True
            return dict(ok=True)
        self.clicks.append(dict(args))
        return dict(ok=self.click_ok, action_sent=True)


class MusicLibTests(unittest.TestCase):
    def test_pick_prefers_the_same_title(self):
        songs = [dict(id=1, name='晴天 (cover)', artists='A', album='', duration=0),
                 dict(id=2, name='晴天', artists='B', album='', duration=0)]
        with mock.patch.object(music_lib, 'search', return_value=dict(ok=True, songs=songs)):
            hit = music_lib.pick('晴天')
        self.assertEqual(hit['song']['id'], 2)
        self.assertTrue(hit['exact'])

    def test_pick_falls_back_to_the_first_result(self):
        songs = [dict(id=9, name='别的歌', artists='', album='', duration=0)]
        with mock.patch.object(music_lib, 'search', return_value=dict(ok=True, songs=songs)):
            hit = music_lib.pick('晴天')
        self.assertEqual(hit['song']['id'], 9)
        self.assertFalse(hit['exact'])

    def test_like_button_detection(self):
        controls = [dict(ref='1', label='取消喜欢', type='Button'),
                    dict(ref='2', label='喜欢', type='Button')]
        self.assertEqual(music_lib.find_like_control(controls)['ref'], '2')
        self.assertTrue(music_lib.already_liked(controls))

    def test_like_needs_a_song_name(self):
        result = music_lib.like_song('')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'need_song')

    def test_like_requires_full_access(self):
        with mock.patch.object(consent, 'is_granted', return_value=False):
            result = music_lib.like_song('晴天')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'no_consent')
        self.assertIn('完整访问', result['error'])

    def _like(self, snapshots, **kwargs):
        session = _FakeSession(snapshots, **kwargs)
        stack = (mock.patch.object(consent, 'is_granted', return_value=True),
                 mock.patch.object(music_lib, 'pick',
                                   return_value=dict(ok=True, song=_SONG)),
                 mock.patch.object(music_lib, '_session',
                                   return_value=(session, threading.Event())),
                 mock.patch.object(music_lib, 'RETRY_TRIES', 2),
                 mock.patch.object(music_lib, 'RETRY_WAIT', 0))
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        return music_lib.like_song('晴天'), session

    def test_like_reports_when_the_heart_is_missing(self):
        result, session = self._like([dict(controls=[], page_id='p', snapshot_id='s')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'no_like_button')
        self.assertFalse(result['action_sent'])
        self.assertFalse(session.clicks)
        self.assertTrue(session.stopped, '结束后要解绑浏览器')

    def test_like_only_succeeds_after_seeing_cancel_like(self):
        result, session = self._like([
            dict(controls=[dict(ref='a', label='喜欢', type='Button')],
                 page_id='p', snapshot_id='s1'),
            dict(controls=[dict(ref='b', label='取消喜欢', type='Button')],
                 page_id='p', snapshot_id='s2')])
        self.assertTrue(result['ok'])
        self.assertTrue(result['verified'])
        self.assertEqual(session.clicks[0]['ref'], 'a')
        self.assertIn('我喜欢的音乐', result['message'])

    def test_like_never_claims_success_without_verification(self):
        result, _session = self._like([
            dict(controls=[dict(ref='a', label='喜欢', type='Button')],
                 page_id='p', snapshot_id='s1'),
            dict(controls=[dict(ref='a', label='喜欢', type='Button')],
                 page_id='p', snapshot_id='s2')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'unverified')
        self.assertTrue(result['action_sent'])

    def test_like_recognises_an_already_liked_song(self):
        result, session = self._like([
            dict(controls=[dict(ref='a', label='取消喜欢', type='Button')],
                 page_id='p', snapshot_id='s1')])
        self.assertTrue(result['ok'])
        self.assertTrue(result.get('already'))
        self.assertFalse(session.clicks)


class HookWebTests(unittest.TestCase):
    def test_site_search_goes_to_the_plugin(self):
        parsed = host_hook.match_rule(_Ctx('在B站搜索猫'))
        self.assertEqual(parsed['action'], 'guga_commander')
        self.assertEqual(parsed['guga_command']['action'], 'site_search')

    def test_known_site_is_left_to_the_native_rule(self):
        # 「打开京东」原生能转成 open_url，插件不能抢。
        self.assertIsNone(host_hook.match_rule(_Ctx('打开京东')))

    def test_plain_search_stays_with_the_native_engine(self):
        # 「搜一下天气」原生走百度，插件解析为 unknown，应当放行。
        self.assertIsNone(host_hook.match_rule(_Ctx('搜一下今天天气')))


class ScriptModeTests(unittest.TestCase):
    def test_script_mode_reads_the_same_permission_switch(self):
        # 直接跑 plugins/guga_commander/*.py 时项目根目录不在 sys.path 上，
        # 早期版本因此把「已允许」判成没授权（CLI 里说 no_consent）。
        here = consent.is_granted()
        code = ("import sys; sys.path.insert(0, %r); import consent; "
                "print(consent.is_granted())" % str(PLUGIN_DIR))
        done = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, encoding='utf-8', cwd=tempfile.gettempdir())
        self.assertEqual(done.stdout.strip(), str(here), done.stderr[-300:])


class CloudMusicFrameTests(unittest.TestCase):
    """坐标换算：CSS 像素 → 本进程坐标 → 屏幕物理像素。"""

    def _frame(self, ratio, dpi=1.5):
        with mock.patch.object(cloudmusic_lib, '_window_rect',
                               return_value=(100, 50, 1600, 1000)), \
             mock.patch.object(cloudmusic_lib, '_dpi_scale', return_value=dpi), \
             mock.patch.object(cloudmusic_lib, '_phys_ratio', return_value=ratio):
            return cloudmusic_lib.Frame(1)

    def test_aware_process_maps_css_times_dpi(self):
        frame = self._frame(ratio=1.0)
        self.assertEqual(frame.coord_per_css, 1.5)
        self.assertEqual(frame.point(465, 37), (798, 106))
        self.assertEqual(frame.box(0, 0, 10, 10), (100, 50, 115, 65))

    def test_unaware_process_keeps_css_units_and_scales_screenshots(self):
        frame = self._frame(ratio=1.5)
        self.assertEqual(frame.coord_per_css, 1.0)
        self.assertEqual(frame.point(465, 37), (565, 87))
        # 截图要的是物理像素，所以整块乘以 1.5
        self.assertEqual(frame.box(0, 0, 10, 10), (150, 75, 165, 90))


class _StubFrame:
    def __init__(self, image):
        self.image = image
        self.boxes = []

    def grab(self, *box):
        self.boxes.append(box)
        return self.image


class CloudMusicPixelTests(unittest.TestCase):
    """判定逻辑：红心变红才算成功，图标亮起来才算悬停在行上。"""

    def test_red_counts_only_the_heart(self):
        image = Image.new('RGB', (32, 26), (14, 16, 20))
        for x in range(10, 22):
            for y in range(8, 18):
                image.putpixel((x, y), (227, 55, 66))
        self.assertGreaterEqual(cloudmusic_lib._red(_StubFrame(image), 400),
                                cloudmusic_lib.RED_MIN)

    def test_grey_heart_is_not_a_like(self):
        image = Image.new('RGB', (32, 26), (14, 16, 20))
        for x in range(10, 22):
            for y in range(8, 18):
                image.putpixel((x, y), (120, 122, 126))
        self.assertLess(cloudmusic_lib._red(_StubFrame(image), 400),
                        cloudmusic_lib.RED_MIN)

    def test_brightness_sees_the_hover_icons(self):
        dark = Image.new('RGB', (60, 18), (12, 13, 16))
        self.assertEqual(cloudmusic_lib._bright(_StubFrame(dark), 400), 0)
        lit = dark.copy()
        for x in range(6, 50):
            lit.putpixel((x, 8), (210, 212, 216))
        self.assertGreaterEqual(cloudmusic_lib._bright(_StubFrame(lit), 400), 12)


class CloudMusicGuardTests(unittest.TestCase):
    """动手之前的门槛：空歌名、没有完整访问，都必须先拦住。"""

    def test_empty_query_is_rejected_before_anything_else(self):
        result = cloudmusic_lib.search('')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'empty_query')

    def test_without_full_access_nothing_is_clicked(self):
        with mock.patch.object(consent, 'is_granted', return_value=False), \
             mock.patch.object(cloudmusic_lib, 'open_client',
                               side_effect=AssertionError('没授权不该去碰客户端')):
            result = cloudmusic_lib.search('晴天')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'no_consent')
        self.assertIn('软件权限', result['error'])


class CloudMusicActionTests(unittest.TestCase):
    """动作编排：搜索 → 找行 → 点 ♥ → 复查。全部打桩，绝不碰真窗口。"""

    def setUp(self):
        self.frame = mock.MagicMock()
        self.frame.hwnd = 1
        self.frame.grab.return_value = Image.new('RGB', (10, 10), (0, 0, 0))
        patcher = mock.patch.object(
            cloudmusic_lib, '_search_only',
            return_value=dict(ok=True, frame=self.frame, hwnd=1, query='晴天 周杰伦'))
        self.search_only = patcher.start()
        self.addCleanup(patcher.stop)
        clicks = mock.patch.object(cloudmusic_lib, '_click')
        self.click = clicks.start()
        self.addCleanup(clicks.stop)

    def test_like_succeeds_only_after_the_heart_turns_red(self):
        with mock.patch.object(cloudmusic_lib, 'stable_row', return_value=400), \
             mock.patch.object(cloudmusic_lib, '_red', side_effect=[0, 0, 60]):
            result = cloudmusic_lib.like('晴天 周杰伦', song='晴天')
        self.assertTrue(result['ok'])
        self.assertTrue(result['verified'])
        self.assertIn('我喜欢的音乐', result['message'])
        self.assertEqual(self.click.call_count, 1)

    def test_like_reports_already_when_the_heart_is_red(self):
        with mock.patch.object(cloudmusic_lib, 'stable_row', return_value=400), \
             mock.patch.object(cloudmusic_lib, '_red', return_value=60):
            result = cloudmusic_lib.like('晴天 周杰伦', song='晴天')
        self.assertTrue(result['ok'])
        self.assertTrue(result['already'])
        self.assertFalse(self.click.called)

    def test_like_without_confirmation_says_so(self):
        with mock.patch.object(cloudmusic_lib, 'stable_row', return_value=400), \
             mock.patch.object(cloudmusic_lib, '_red', return_value=0):
            result = cloudmusic_lib.like('晴天 周杰伦', song='晴天')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'unverified')
        self.assertIn('没确认到', result['error'])

    def test_missing_row_never_blind_clicks(self):
        with mock.patch.object(cloudmusic_lib, 'stable_row', return_value=0):
            result = cloudmusic_lib.like('晴天 周杰伦', song='晴天')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'ui_not_found')
        self.assertFalse(self.click.called)

    def test_play_verifies_by_the_playbar_changing(self):
        with mock.patch.object(cloudmusic_lib, 'stable_row', return_value=400), \
             mock.patch.object(cloudmusic_lib, '_same', return_value=False):
            result = cloudmusic_lib.play('晴天 周杰伦')
        self.assertTrue(result['ok'])
        self.assertTrue(result['verified'])

    def test_play_without_change_is_not_reported_as_success(self):
        with mock.patch.object(cloudmusic_lib, 'stable_row', return_value=400), \
             mock.patch.object(cloudmusic_lib, '_same', return_value=True):
            result = cloudmusic_lib.play('晴天 周杰伦')
        self.assertFalse(result['ok'])
        self.assertIn('没变化', result['error'])


class MusicPlayParseTests(unittest.TestCase):
    def test_platform_play_goes_to_the_client(self):
        cmd = commander.parse('用网易云播放晴天')
        self.assertEqual(cmd.action, 'music_play')
        self.assertEqual(cmd.target, '晴天')

    def test_play_without_a_song_asks_which(self):
        cmd = commander.parse('在网易云播放')
        self.assertEqual(cmd.action, 'music_play')
        self.assertEqual(cmd.target, '')
        self.assertIn('哪一首', cmd.note)

    def test_demonstrative_is_not_a_song_name(self):
        self.assertEqual(commander.parse('用网易云播放这首歌').target, '')

    def test_opening_the_client_is_still_open_app(self):
        self.assertEqual(commander.parse('打开网易云音乐').action, 'open_app')


if __name__ == '__main__':
    unittest.main(verbosity=2)
