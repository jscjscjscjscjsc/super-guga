# -*- coding: utf-8 -*-
"""咕嘎指令台插件：一句中文打开应用 / 开网站并直接搜 / 在网易云客户端里搜歌收藏 / 让咕嘎自己玩。

这个包是纯标准库实现，导入它不会启动 Qt、不会连网、不会执行任何动作。
真正接入桌宠由 ``install.py`` 完成（可随时卸载还原）。
"""
from .commander import (  # noqa: F401
    PLUGIN_ID,
    VERSION,
    Command,
    parse,
    plan_for,
    run,
    bind,
    find_runtime,
    default_call,
    market_card,
    HELP_TEXT,
)

__all__ = [
    'PLUGIN_ID', 'VERSION', 'Command', 'parse', 'plan_for', 'run',
    'bind', 'find_runtime', 'default_call', 'market_card', 'HELP_TEXT',
]
