# -*- coding: utf-8 -*-
"""咕嘎指令台 · 一键安装 / 卸载。

    python install.py              # 安装
    python install.py --status     # 查看状态
    python install.py --uninstall  # 卸载并还原

安装做三件事：

1. 在 ``core/fast_commands.py`` **末尾追加**一段钩子（不改动任何原有代码行），
   钩子在运行时把插件规则插到原生 ``_match_open_app`` 之前——这样裸游戏名
   （「打开大表哥2」）能先经过 Steam 库匹配；
2. 在 ``main.py`` 的 ``main()`` 里**插入**一段启动授权弹窗（同样带标记、可精确删除），
   桌宠一启动就问「能不能读取本机文件」；
3. 生成 ``plugins/community/guga-commander.json`` 插件市场卡片。

安全设计：

* 全程按**字节**读写，保留原文件的换行符风格（CRLF/LF 都不被改动）；
* 改动前把原文件原样备份成 ``*.guga-commander.bak``；
* 改动后立刻用 ``compile()`` 做语法校验，不通过就回滚；
* 用标记行判定是否已安装，重复执行不会重复追加；
* 卸载优先用备份字节级还原，没有备份才按标记块切除；
* 两处补丁整体包在 try/except 里，即使插件文件被删，项目也照常运行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PLUGIN_DIR.parents[1]
FAST_COMMANDS = PROJECT_ROOT / 'core' / 'fast_commands.py'
BACKUP = PROJECT_ROOT / 'core' / 'fast_commands.py.guga-commander.bak'
CARD = PROJECT_ROOT / 'plugins' / 'community' / 'guga-commander.json'

HOOK_BEGIN = b'# --- guga-commander plugin hook (managed by plugins/guga_commander/install.py) ---'
HOOK_END = b'# --- end guga-commander plugin hook ---'

STARTUP_ANCHOR = b'    sys.exit(app.exec_())'
STARTUP_BEGIN = b'    # --- guga-commander startup consent (managed by plugins/guga_commander/install.py) ---'
STARTUP_END = b'    # --- end guga-commander startup consent ---'

STARTUP_BLOCK = '''    # --- guga-commander startup consent (managed by plugins/guga_commander/install.py) ---
    # 启动时询问本机文件权限：允许才读取/打开本机文件，拒绝则整项能力关闭。
    # 延迟 0.7 秒，等桌宠窗口先画出来；任何异常都不会影响启动。
    try:
        from plugins.guga_commander.startup import ask_at_startup as _guga_ask_permission
        QTimer.singleShot(700, lambda: _guga_ask_permission(window))
    except Exception:
        pass
    # --- end guga-commander startup consent ---
'''

HOOK_BLOCK = '''

# --- guga-commander plugin hook (managed by plugins/guga_commander/install.py) ---
# 只追加，不改动上方任何原有逻辑；卸载时按这对标记整块移除。
# 插件规则插到原生 _match_open_app **之前**：裸游戏名（「打开大表哥2」）必须先过
# Steam 库匹配，否则会被原生 open_app 当成软件名报「未找到该软件」。插件对非
# Steam 目标一律返回 None，所以「打开微信」等既有路由完全不受影响。
try:
    from plugins.guga_commander import host_hook as _guga_hook
    try:
        _guga_at = _MATCH_RULES.index(_match_open_app)
    except (NameError, ValueError):
        _guga_at = len(_MATCH_RULES)
    _MATCH_RULES = (tuple(_MATCH_RULES[:_guga_at]) + (_guga_hook.match_rule,)
                    + tuple(_MATCH_RULES[_guga_at:]))
    _guga_original_execute = _execute

    def _execute(result, computer=None):
        values = result if isinstance(result, dict) else {}
        if values.get('action') == 'guga_commander':
            try:
                return _guga_hook.execute(values)
            except Exception as _guga_exc:
                return dict(ok=False, error='guga-commander: %s: %s'
                            % (type(_guga_exc).__name__, str(_guga_exc)[:200]))
        return _guga_original_execute(result, computer)
except Exception:
    pass
# --- end guga-commander plugin hook ---
'''


def _main_py() -> Path:
    """路径按 PROJECT_ROOT 现算：测试把 PROJECT_ROOT 指到临时目录时，就不会碰到真实文件。"""
    return Path(PROJECT_ROOT) / 'main.py'


def _main_backup() -> Path:
    return Path(PROJECT_ROOT) / 'main.py.guga-commander.bak'


def _block_bytes(sample: bytes) -> bytes:
    """让追加块沿用原文件的换行符风格。"""
    data = HOOK_BLOCK.encode('utf-8')
    if b'\r\n' in sample[:8192]:
        data = data.replace(b'\n', b'\r\n')
    return data


def _startup_bytes(sample: bytes) -> bytes:
    data = STARTUP_BLOCK.encode('utf-8')
    if b'\r\n' in sample[:8192]:
        data = data.replace(b'\n', b'\r\n')
    return data


def _install_startup() -> bool:
    """在 main.py 的 sys.exit(app.exec_()) 之前插入授权弹窗。"""
    main_py = _main_py()
    if not main_py.is_file():
        print('警告：找不到 main.py，跳过启动询问')
        return False
    original = main_py.read_bytes()
    if STARTUP_BEGIN in original:
        print('启动授权弹窗已存在，跳过插入（幂等）')
        return True
    if STARTUP_ANCHOR not in original:
        print('警告：main.py 结构变了（找不到插入锚点），跳过启动询问；'
              '其余功能不受影响')
        return False
    backup = _main_backup()
    if not backup.is_file():
        backup.write_bytes(original)
        print('已备份原文件 ->', backup.name)
    patched = original.replace(STARTUP_ANCHOR, _startup_bytes(original) + STARTUP_ANCHOR, 1)
    try:
        compile(patched, str(main_py), 'exec')
    except SyntaxError as exc:
        print('main.py 语法校验失败，已跳过启动询问：', exc)
        return False
    main_py.write_bytes(patched)
    print('已在 main.py 插入启动授权弹窗（原有代码一行未改）')
    return True


def _write_card(card: dict) -> None:
    CARD.parent.mkdir(parents=True, exist_ok=True)
    CARD.write_text(json.dumps(card, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')


def status() -> dict:
    source = FAST_COMMANDS.read_bytes() if FAST_COMMANDS.is_file() else b''
    main_source = _main_py().read_bytes() if _main_py().is_file() else b''
    return dict(
        project_root=str(PROJECT_ROOT),
        hook_installed=HOOK_BEGIN in source,
        backup_exists=BACKUP.is_file(),
        startup_prompt=STARTUP_BEGIN in main_source,
        main_backup=_main_backup().is_file(),
        card_exists=CARD.is_file(),
        plugin_files=sorted(p.name for p in PLUGIN_DIR.glob('*.py')),
    )


def install() -> int:
    if not FAST_COMMANDS.is_file():
        print('找不到 core/fast_commands.py，请确认项目路径：', PROJECT_ROOT)
        return 1

    original = FAST_COMMANDS.read_bytes()

    # 1) 钩子：幂等追加 + 备份 + 语法校验
    if HOOK_BEGIN not in original:
        if not BACKUP.is_file():
            BACKUP.write_bytes(original)
            print('已备份原文件 ->', BACKUP.name)
        patched = original.rstrip(b'\r\n') + _block_bytes(original)
        try:
            compile(patched, str(FAST_COMMANDS), 'exec')
        except SyntaxError as exc:
            print('语法校验失败，已中止安装（原文件未改动）：', exc)
            return 1
        FAST_COMMANDS.write_bytes(patched)
        print('已在 core/fast_commands.py 末尾追加插件钩子（原有代码一行未改）')
    else:
        print('插件钩子已存在，跳过追加（幂等）')

    # 2) 启动授权弹窗（插到 main.py 的 app.exec_() 之前）
    _install_startup()

    # 3) 插件市场卡片
    sys.path.insert(0, str(PROJECT_ROOT))
    from plugins.guga_commander.commander import market_card, parse, PLUGIN_ID  # noqa: E402
    card = market_card()
    if CARD.is_file():
        try:
            existing = json.loads(CARD.read_text(encoding='utf-8'))
            if existing.get('id') != PLUGIN_ID:
                print('警告：已存在同名但不同 id 的卡片，未覆盖 ->', CARD)
            else:
                _write_card(card)
                print('已更新插件市场卡片 -> plugins/community/guga-commander.json')
        except (ValueError, OSError):
            print('警告：已有卡片无法解析，未覆盖 ->', CARD)
    else:
        _write_card(card)
        print('已生成插件市场卡片 -> plugins/community/guga-commander.json')

    # 4) 自检：确认插件能被导入、解析正常
    probe = parse('自己打一局战棋')
    if probe.action != 'auto_play':
        print('自检失败：命令解析结果异常', probe.as_dict())
        return 1
    print('自检通过：「自己打一局战棋」 ->', probe.action, probe.game)
    print('\n安装完成。启动咕嘎后可以直接说：')
    print('  打开微信 / 把记事本打开 / 自己打一局战棋 / 看看打到哪了 / 停一下')
    print('  打开 steam / 打开 steam 上的巫师3 / 玩一下大表哥2 / 我 steam 里有什么游戏')
    print('  打开 D:\\报告.pdf / 打开桌面上的合同 / 允许读取文件 / 关闭文件权限')
    print('启动时会弹窗询问「能否读取本机文件」，选「允许」后文件功能才可用。')
    print('查看状态：python install.py --status    卸载：python install.py --uninstall')
    return 0


def uninstall() -> int:
    if not FAST_COMMANDS.is_file():
        print('找不到 core/fast_commands.py')
        return 1
    source = FAST_COMMANDS.read_bytes()

    if HOOK_BEGIN in source:
        if BACKUP.is_file():
            FAST_COMMANDS.write_bytes(BACKUP.read_bytes())
            print('已用备份字节级还原 core/fast_commands.py')
        else:
            before, _, rest = source.partition(HOOK_BEGIN)
            _, _, after = rest.partition(HOOK_END)
            if not after:
                print('钩子结束标记缺失，为安全起见未修改文件，请手动检查')
                return 1
            # 标记块后面还可能跟着别人新加的代码，原样保留。
            tail = after.lstrip(b'\r\n')
            restored = before.rstrip(b'\r\n')
            if tail:
                restored += b'\n' + tail
            else:
                restored += b'\n'
            try:
                compile(restored, str(FAST_COMMANDS), 'exec')
            except SyntaxError as exc:
                print('还原后语法校验失败，未写入：', exc)
                return 1
            FAST_COMMANDS.write_bytes(restored)
            print('已按标记移除钩子（无备份，采用切除方式）')
    else:
        print('钩子未安装，无需处理 core/fast_commands.py')

    # main.py：启动授权弹窗
    main_py, main_backup = _main_py(), _main_backup()
    if main_py.is_file():
        main_source = main_py.read_bytes()
        if STARTUP_BEGIN in main_source:
            if main_backup.is_file():
                main_py.write_bytes(main_backup.read_bytes())
                print('已用备份字节级还原 main.py')
            else:
                head, _, rest = main_source.partition(STARTUP_BEGIN)
                _, _, tail = rest.partition(STARTUP_END)
                restored = head + tail.lstrip(b'\r\n')
                try:
                    compile(restored, str(main_py), 'exec')
                except SyntaxError as exc:
                    print('main.py 还原后语法校验失败，未写入：', exc)
                    return 1
                main_py.write_bytes(restored)
                print('已按标记移除 main.py 里的启动弹窗')
        else:
            print('启动弹窗未安装，无需处理 main.py')
        if main_backup.is_file():
            main_backup.unlink()
            print('已清理 main.py 备份')

    # 只移除本插件自己生成的卡片
    if CARD.is_file():
        try:
            data = json.loads(CARD.read_text(encoding='utf-8'))
            if data.get('id') == 'guga-commander':
                CARD.unlink()
                print('已移除插件市场卡片')
            else:
                print('卡片 id 不匹配，保留未动 ->', CARD)
        except (ValueError, OSError):
            print('卡片无法解析，保留未动 ->', CARD)

    if BACKUP.is_file():
        BACKUP.unlink()
        print('已清理备份文件')

    print('\n卸载完成，项目已回到安装前的状态。')
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if '--status' in args:
        for key, value in status().items():
            print('%-18s %s' % (key + ':', value))
        return 0
    if '--uninstall' in args:
        return uninstall()
    return install()


if __name__ == '__main__':
    raise SystemExit(main())
