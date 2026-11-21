# -*- coding: utf-8 -*-
"""宿主钩子：只被 ``core/fast_commands.py`` 末尾那段追加代码调用。

钩子被插到原生 ``_match_open_app`` **之前**（位置由 install.py 决定），原因只有一个：
「打开黑神话悟空」「打开大表哥2」这种裸游戏名必须先过一次 Steam 库匹配，
否则会被原生 open_app 当成软件名，报「未找到该软件」。

除此之外不改变任何既有优先级：非 Steam 目标一律返回 None 交回原生规则，
「打开微信」仍然走项目自带的逻辑。
"""
from __future__ import annotations

from .commander import Command, parse, run


def _host_handles(name: str) -> bool:
    """原生 open_app 是否能**真的启动**这个名字。

    注意不能只看「是不是桌面软件」：原生 ``_match_open_app`` 里还有一道
    ``_unresolved_site_candidate``——凡是不在它名单里、又像网址的名字
    （steam、网易云音乐、大表哥2）都会被判成「交给智能体」，于是落到模型上。
    所以这里必须是「原生确实能启动」才算接得住。
    """
    try:
        from core.fast_commands import APP_ALIASES, BROWSER_NAMES, _is_known_local_app
    except Exception:  # noqa: BLE001 - 读不到宿主表就保守放行，不抢
        return True
    value = str(name or '').strip()
    if not value:
        return True
    if value in BROWSER_NAMES or value in APP_ALIASES:
        return True
    try:
        return bool(_is_known_local_app(value))
    except Exception:  # noqa: BLE001
        return True


def _site_alias(name: str) -> bool:
    """原生会把它转成网址打开的（京东、b站…）。这类要原样放行，让 ``open_url`` 生效。"""
    try:
        from core.fast_commands import SITE_ALIASES
    except Exception:  # noqa: BLE001
        return False
    return str(name or '').strip().casefold() in SITE_ALIASES


def _installed_app(name: str):
    """本机确实装着的软件 -> 命中信息；否则 None。全部异常都当作「没找到」。"""
    try:
        from . import app_lib
        hit = app_lib.resolve(name)
    except Exception:  # noqa: BLE001 - 插件不能因为扫开始菜单出错就吃掉这句话
        return None
    if hit.get('ok') or hit.get('candidates'):
        return hit
    return None


def match_rule(ctx):
    """一条快速命令规则。返回 None 表示「这句话不归我管」。

    ``ctx`` 是 ``core.fast_commands._CommandText``，有 ``value``（归一化文本）、
    ``raw``（原话）、``prefix``（含 source_text 的公共字段）。
    """
    try:
        value = str(getattr(ctx, 'value', '') or getattr(ctx, 'raw', '') or '')
        prefix = dict(getattr(ctx, 'prefix', {}) or {})
        cmd = parse(value)
        if cmd.action == 'unknown':
            return None

        if cmd.action == 'open_app':
            # 网站类目标（京东、b站）要留给原生转成 open_url，插件不能接。
            if _site_alias(cmd.target):
                return None
            # 原生确实能启动的（微信、记事本…）：原样交回，享受它的后台线程
            # 启动与窗口复用。
            if _host_handles(cmd.target):
                return dict(prefix, matched=True, action='open_app', silent=True,
                            priority='fast', kind='app', target=cmd.target)
            # 原生接不住、但本机真的装着：插件直接启动，绝不交给模型。
            # 这一步是「打开 steam / 打开网易云音乐」报 DNS 错误的根治点。
            if _installed_app(cmd.target):
                local = Command(action='open_local_app', target=cmd.target, raw=cmd.raw)
                return dict(prefix, matched=True, action='guga_commander', silent=False,
                            priority='fast', kind='guga_commander', target=value,
                            guga_command=local.as_dict())
            # 本机也没有：放行，让原生规则或智能体如实说明。
            return None

        return dict(prefix, matched=True, action='guga_commander', silent=False,
                    priority='fast', kind='guga_commander', target=value,
                    guga_command=cmd.as_dict())
    except Exception:  # noqa: BLE001 - 钩子出错必须静默退回原生规则
        return None


def execute(result):
    """执行 ``action='guga_commander'`` 的命令，返回宿主可直接播报的回执。"""
    data = (result or {}).get('guga_command') or {}
    cmd = Command.from_dict(data)
    if cmd.action == 'unknown':
        cmd = parse(str((result or {}).get('target') or ''))
    answer = run(cmd)
    # 宿主 fast_commands.execute() 会把这个 dict 原样交给播报逻辑。
    answer.setdefault('kind', 'guga_commander')
    return answer
