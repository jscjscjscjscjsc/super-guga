"""本地软件解析：把一句中文软件名，变成一个真的能启动的东西。

为什么需要这个模块
------------------
项目原生的 ``_open_app`` 只认它自己登记过的软件（``runtime/chat/authorized_apps.json``
+ 少量别名表）。不在名单里的软件（网易云音乐、QQ音乐、钉钉…）会被当成「像网站的名字」
交给大模型，而模型一旦不可用，用户看到的就是「模型服务域名解析失败」——明明本机装着，
却打不开。

本模块只做一件事：**在动手之前先确认这个软件真的存在**，找得到就返回真实路径，
找不到就老老实实返回 None，让原生逻辑和智能体照旧处理。

三个来源，按可靠性排序
----------------------
1. 注册表 ``App Paths``：软件安装时登记的 exe 绝对路径，最可靠；
2. 开始菜单快捷方式（.lnk）：中文名最全，直接 ``os.startfile`` 就能启动；
3. 少量人工别名（``EXE_HINTS``）：把「网易云」和 ``cloudmusic.exe`` 对上。

全部只读、纯标准库、不联网。带 TTL 缓存，避免每句话都扫开始菜单。
"""
from __future__ import annotations

import os
import re
import time

try:  # Windows 专有，测试环境要能 import
    import winreg
except ImportError:  # pragma: no cover - 非 Windows
    winreg = None

# 语料里常见的软件中文名 -> 安装后 exe 名。只为「中文名和文件名完全不像」的软件准备，
# 其余靠开始菜单 / App Paths 自动发现，不需要在这里登记。
EXE_HINTS = {
    '网易云音乐': ('cloudmusic.exe',),
    'qq音乐': ('qqmusic.exe',),
    '酷狗音乐': ('kugou.exe',),
    '酷我音乐': ('kwmusic.exe',),
    '企业微信': ('wxwork.exe',),
    '钉钉': ('dingtalk.exe',),
    '飞书': ('feishu.exe',),
    '腾讯会议': ('wemeetapp.exe',),
    '百度网盘': ('baidunetdisk.exe',),
    '迅雷': ('thunder.exe',),
    '剪映': ('jianyingpro.exe',),
    '哔哩哔哩': ('bilibili.exe',),
    '抖音': ('douyin.exe',),
    '小红书': ('xiaohongshu.exe',),
    '有道词典': ('youdaodict.exe',),
    '搜狗输入法': ('sogouinput.exe',),
}

# 快捷方式里出现这些词的一律当「不是启动项」：卸载程序、帮助文档、官网链接。
_SKIP_WORDS = ('卸载', 'uninstall', '帮助', 'help', 'readme', '说明', '手册',
               '教程', '官网', '更新', '修复', 'license', '许可')

_PUNCT = re.compile(r'[\s\-_·・.,，。:：;；!！?？()（）\[\]【】"\'“”‘’《》<>/\\|]+')

_CACHE = {'at': 0.0, 'index': []}
_CACHE_TTL = 300.0


def _norm(text: str) -> str:
    """归一化：去空格标点、去扩展名、全角转半角后的大小写折叠。"""
    value = str(text or '').strip()
    value = value.replace('\u3000', ' ')
    for ext in ('.lnk', '.exe', '.url', '.appref-ms'):
        if value.lower().endswith(ext):
            value = value[: -len(ext)]
    return _PUNCT.sub('', value).casefold()


def _start_menu_roots():
    roots = []
    for env in ('APPDATA', 'PROGRAMDATA'):
        base = os.environ.get(env)
        if not base:
            continue
        path = os.path.join(base, 'Microsoft', 'Windows', 'Start Menu', 'Programs')
        if os.path.isdir(path):
            roots.append(path)
    return roots


def _walk_shortcuts(root: str, max_depth: int = 3):
    """遍历开始菜单，收集 .lnk / .exe。深度有限，避免用户把软件目录挂到开始菜单里。"""
    root = os.path.abspath(root)
    base_depth = root.count(os.sep)
    for current, dirs, files in os.walk(root):
        if current.count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        for name in files:
            lower = name.lower()
            if not (lower.endswith('.lnk') or lower.endswith('.exe')):
                continue
            if any(word in lower for word in _SKIP_WORDS):
                continue
            yield os.path.join(current, name)


def _app_paths():
    """注册表 App Paths：{'cloudmusic.exe': 'D:\\...\\cloudmusic.exe'}"""
    if winreg is None:
        return {}
    locations = (
        (winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\App Paths'),
        (winreg.HKEY_LOCAL_MACHINE, r'Software\Microsoft\Windows\CurrentVersion\App Paths'),
        (winreg.HKEY_LOCAL_MACHINE,
         r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths'),
    )
    found = {}
    for hive, path in locations:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            total = winreg.QueryInfoKey(key)[0]
            for i in range(total):
                try:
                    exe = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, exe) as item:
                        target = str(winreg.QueryValueEx(item, '')[0] or '').strip('"')
                except OSError:
                    continue
                if target and os.path.isfile(target):
                    found.setdefault(exe.casefold(), target)
        finally:
            key.Close()
    return found


def index(force: bool = False):
    """全部可启动项：[{name, path, kind, norm}]。带 TTL 缓存。"""
    now = time.monotonic()
    if not force and _CACHE['index'] and now - _CACHE['at'] < _CACHE_TTL:
        return list(_CACHE['index'])

    items = []
    seen = set()
    for exe, path in _app_paths().items():
        name = os.path.splitext(os.path.basename(path))[0]
        items.append(dict(name=name, path=path, kind='exe', norm=_norm(name)))
        seen.add(os.path.normcase(path))

    for root in _start_menu_roots():
        for path in _walk_shortcuts(root):
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            name = os.path.splitext(os.path.basename(path))[0]
            items.append(dict(name=name, path=path, kind='lnk', norm=_norm(name)))

    _CACHE.update(at=now, index=items)
    return list(items)


def _score(query: str, candidate: str) -> int:
    """名字相似度。要求「整串包含」而不是逐字分散匹配，避免误开。"""
    if not query or not candidate:
        return 0
    if query == candidate:
        return 100
    if candidate.startswith(query):
        return 85 - min(len(candidate) - len(query), 20)
    if query in candidate:
        return 65 - min(len(candidate) - len(query), 15)
    if candidate in query:
        return 50 - min(len(query) - len(candidate), 10)
    return 0


def resolve(name: str, items=None):
    """软件名 -> 解析结果。

    返回 ``{ok, path, name, kind, candidates, error}``：
    - 唯一命中：``ok=True``，带真实路径；
    - 多个同分候选：``ok=False`` + ``candidates``，让用户说清楚是哪一个；
    - 没有：``ok=False``，``error`` 为空，方便调用方静默退回原生逻辑。
    """
    query = _norm(name)
    if not query:
        return dict(ok=False, error='', path='', name='', candidates=[])

    pool = list(items) if items is not None else index()

    # 别名优先：中文名和 exe 名完全不像的软件（网易云音乐 -> cloudmusic.exe）
    for hint in EXE_HINTS.get(str(name).strip().casefold(), ()):
        hit = next((it for it in pool
                    if os.path.basename(it['path']).casefold() == hint.casefold()), None)
        if hit:
            return dict(ok=True, path=hit['path'], name=hit['name'], kind=hit['kind'],
                        candidates=[hit], error='')

    scored = []
    for item in pool:
        value = _score(query, item.get('norm') or _norm(item['name']))
        if value > 0:
            scored.append((value, item))
    if not scored:
        return dict(ok=False, path='', name='', candidates=[], error='')

    scored.sort(key=lambda pair: (-pair[0], len(pair[1]['name'])))
    top = scored[0][0]
    best = [item for value, item in scored if value == top]

    # 同分但其实是同一个程序（开始菜单快捷方式 + 注册表 exe），按名字去重
    unique = {}
    for item in best:
        unique.setdefault(item['norm'], item)
    best = list(unique.values())

    if len(best) == 1:
        item = best[0]
        return dict(ok=True, path=item['path'], name=item['name'], kind=item['kind'],
                    candidates=[item], error='')
    return dict(ok=False, path='', name='', kind='',
                candidates=[dict(name=it['name'], path=it['path']) for it in best[:6]],
                error='')


def launch(name: str, items=None) -> dict:
    """启动软件。只做「请 Windows 启动」这一件事，失败如实返回原因。"""
    hit = resolve(name, items)
    if hit.get('ok'):
        try:
            os.startfile(hit['path'])  # noqa: S606 - 这是本模块的唯一职责
        except OSError as exc:
            return dict(ok=False, error='启动 %s 失败：%s' % (hit['name'], exc),
                        path=hit['path'])
        return dict(ok=True, path=hit['path'], app=hit['name'],
                    message='已请 Windows 启动《%s》。' % hit['name'])
    names = [c['name'] for c in hit.get('candidates') or []]
    if names:
        return dict(ok=False, code='ambiguous',
                    error='找到多个可能的软件：%s。请说清楚是哪一个，或直接说它的完整名称。'
                          % '、'.join(names[:6]),
                    candidates=names)
    return dict(ok=False, code='not_found', error='本机没有找到名为「%s」的软件。' % name)


def available() -> list:
    return sorted({item['name'] for item in index()}, key=str.casefold)


if __name__ == '__main__':  # pragma: no cover - 手工排查用
    import sys

    if len(sys.argv) > 1:
        print(launch(sys.argv[1]))
    else:
        everything = index(force=True)
        print('可启动项 %d 个（开始菜单 + App Paths）' % len(everything))
        for item in everything[:40]:
            print('  %-34s %s' % (item['name'][:33], item['path']))
