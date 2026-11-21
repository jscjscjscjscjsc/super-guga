# -*- coding: utf-8 -*-
"""本机文件权限：把「启动时问一次」的结果，变成一个所有地方都认的开关。

为什么不自己发明一套权限
------------------------
项目原本就有「完整访问」开关：``runtime/chat/local_access.json`` 里的
``full_access``，由 ``core/desktop_access.py`` 读取，控制文件读写、限时命令、
屏幕与输入。**本插件不另建开关**，只是把它的值同步过来，这样：

* 用户在启动弹窗里选「允许」 → 完整访问打开，插件的「打开文件」也放行；
* 选「不允许」 → 完整访问关掉，插件拒绝读写，项目自己的文件工具也一起关掉；
* 用户在「软件权限」面板里改，也仍然是同一个开关，不会两套状态打架。

额外的 ``file_access.json`` 只记录「问过没有、上次选了什么」，用来决定弹窗的
默认按钮和文案，不参与权限判断（判断始终以开关为准，记录里的显式拒绝除外）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE = PROJECT_ROOT / 'runtime' / 'guga_commander' / 'file_access.json'
CORE_STATE = PROJECT_ROOT / 'runtime' / 'chat' / 'local_access.json'


def _read() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> dict:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    temporary.replace(STATE)
    return data


def _now() -> str:
    # 毫秒：判断「面板是不是在那之后被改过」时，秒级精度不够。
    return datetime.now().isoformat(timespec='milliseconds')


def _access(state_path=None):
    """项目原生的「完整访问」开关；导入失败返回 None（调用方按拒绝处理）。"""
    try:
        from core.desktop_access import DesktopAccess
    except ImportError:
        # 直接 `python plugins/guga_commander/xxx.py` 时项目根目录不在 sys.path 上，
        # 不补这一步会把「已允许」误判成没授权（踩过）。
        import sys
        root = str(PROJECT_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from core.desktop_access import DesktopAccess
        except Exception:  # noqa: BLE001 - 宿主导不进就按拒绝处理
            return None
    except Exception:  # noqa: BLE001 - 插件不能因为宿主改结构就崩
        return None
    return DesktopAccess(state_path) if state_path else DesktopAccess()


def core_enabled(state_path=None) -> bool:
    """原生开关当前是否打开。读不到就当作关闭——权限判断一律从严。"""
    access = _access(state_path)
    if access is None:
        return False
    try:
        return bool(access.enabled)
    except Exception:  # noqa: BLE001
        return False


def core_state_is_explicit() -> bool:
    """用户是否在「软件权限」里显式动过这个开关（文件存在即为动过）。"""
    return CORE_STATE.exists()


def _panel_changed_since(timestamp: str) -> bool:
    """「完整访问」开关是否在那之后被改过——也就是用户又去面板点了一次。"""
    if not timestamp:
        return False
    try:
        moment = datetime.fromisoformat(str(timestamp)).timestamp()
        # 留 0.5 秒余量：拒绝那一刻顺手写下的旧开关状态不算「又点了一次」。
        return CORE_STATE.stat().st_mtime > moment + 0.5
    except (OSError, ValueError, TypeError):
        return False


def is_granted() -> bool:
    """当前是否允许插件读取 / 打开本机文件。

    从严判断：必须是「原生开关打开」+「用户确实表达过同意」。
    用户没被问过、也没在设置里开过，就当作没授权。

    例外只有一条：明确拒绝之后，用户又自己去「软件权限」把开关打开了——
    那是新的同意，不能因为旧记录把人卡死（弹窗里就是这么告诉用户的）。
    """
    data = _read()
    core = core_enabled()
    if not core:
        return False
    if data.get('granted') is False:
        return _panel_changed_since(data.get('denied_at', ''))
    if data.get('granted') is True:
        return True
    return core_state_is_explicit()  # 用户自己勾过「完整访问」也算同意


def set_granted(granted: bool, source: str = 'startup') -> dict:
    """写入授权结果：同步原生开关 + 记录本次选择。"""
    granted = bool(granted)
    access = _access()
    if access is not None:
        try:
            access.set_enabled(granted)
        except Exception:  # noqa: BLE001 - 写不了开关也不能让启动流程炸掉
            pass
    data = _read()
    data['granted'] = granted
    data['source'] = str(source)
    data['version'] = 1
    if granted:
        data['granted_at'] = _now()
        data.pop('denied_at', None)
    else:
        data['denied_at'] = _now()
    return _write(data)


def asked_before() -> bool:
    """是否已经问过（问过一次就不再打扰）。"""
    data = _read()
    return bool(data.get('asked_at') or data.get('granted_at')
                or data.get('denied_at') or 'granted' in data)


def should_ask() -> bool:
    """启动时要不要弹窗：只在「从没问过 + 用户没自己动过开关」时问一次。

    之后无论允许还是拒绝都安静启动；想改权限去「软件权限…」，
    或者直接对我说「允许读取文件」。
    """
    if asked_before():
        return False
    return not core_state_is_explicit()


def mark_asked(choice: str = '') -> dict:
    """只记录「这次启动问过了」，不改授权。"""
    data = _read()
    data['asked_at'] = _now()
    data['asks'] = int(data.get('asks') or 0) + 1
    if choice:
        data['last_choice'] = str(choice)
    return _write(data)


def status() -> dict:
    data = _read()
    return dict(granted=is_granted(), core_full_access=core_enabled(),
                core_explicit=core_state_is_explicit(),
                asked_at=data.get('asked_at', ''), granted_at=data.get('granted_at', ''),
                denied_at=data.get('denied_at', ''), asks=data.get('asks', 0),
                state_file=str(STATE))


NO_PERMISSION_HINT = (
    '本机文件权限没有开启，所以没有读取任何文件。\n'
    '开启方式：点对话窗口右上角的「···」→「软件权限…」，'
    '勾选「允许咕嘎完整访问本机」；也可以直接对我说「允许读取文件」。'
)
