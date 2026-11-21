# -*- coding: utf-8 -*-
"""打开本机文件 / 文件夹：先认路径，再验权限，最后才动手。

三条硬规矩
----------
1. **先问权限**：没授权直接拒绝，一个字节都不读（见 ``consent.py``）；
2. **只开存在的**：路径必须真实存在，找不到就列出候选，绝不瞎猜；
3. **不递归扫全盘**：只在桌面/文档/下载等常用目录里找，深度和数量都有上限，
   避免「打开报告.pdf」变成一次全盘遍历。

打开动作只用 ``os.startfile``——等同于用户在资源管理器里双击，不注入进程、
不提权、不改文件。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

try:
    from . import consent
except ImportError:  # 单文件调试
    import consent

# 常用目录：中文说法 -> 磁盘上可能的文件夹名（中文系统里通常仍是英文名）
COMMON_DIRS = (
    ('桌面', ('Desktop', '桌面')),
    ('文档', ('Documents', '文档')),
    ('下载', ('Downloads', '下载')),
    ('图片', ('Pictures', '图片')),
    ('音乐', ('Music', '音乐')),
    ('视频', ('Videos', '视频')),
)

# 出现盘符、UNC、家目录或相对路径前缀，就当作用户在说路径
_PATH_RE = re.compile(r'^(?:[a-zA-Z]:[\\/]|\\\\|~[\\/]|\.{1,2}[\\/])')
# 出现这些扩展名，即使没写目录也认为是在说文件
_EXT_RE = re.compile(
    r'\.(?:txt|md|pdf|docx?|xlsx?|pptx?|csv|json|ya?ml|ini|log|py|js|ts|html?|css|'
    r'png|jpe?g|gif|webp|bmp|svg|ico|mp4|mkv|avi|mov|wmv|flv|mp3|wav|flac|ape|'
    r'zip|rar|7z|tar|gz|exe|msi|lnk|bat|cmd|ps1|reg)$', re.I)

_MAX_DEPTH = 3          # 常用目录下最多看三层
_MAX_SCAN = 4000        # 单次最多看这么多条目录项
_MAX_CANDIDATES = 6

# 这些目录不进去：太深、太大，且基本不会是用户要找的「我的文件」
_SKIP_DIRS = {'AppData', 'Application Data', 'node_modules', '.git', '$RECYCLE.BIN',
              'Cookies', 'Local Settings', 'NetHood', 'PrintHood', 'Recent',
              'SendTo', 'Templates', 'Windows', 'Program Files', 'Program Files (x86)'}


def clean_text(text: str) -> str:
    """去掉口语里的包装：引号、书名号、「文件」前缀、结尾语气词。"""
    value = str(text or '').strip()
    value = value.strip('“”"\'「」『』《》<>')
    value = re.sub(r'^(?:一下|这个|那个|我的)?\s*(?:文件|文件夹|目录)\s*[：:]\s*', '', value)
    value = re.sub(r'(?:吧|呀|呢|啊|啦|谢谢|一下)$', '', value).strip()
    return value.strip('“”"\'「」『』《》<> ')


def looks_like_path(text: str) -> bool:
    """这句话像不像在说本机路径（而不是软件名）。"""
    value = clean_text(text)
    if not value:
        return False
    if _PATH_RE.match(value) or _EXT_RE.search(value):
        return True
    return folder_hint(value)[0] is not None


def folder_hint(text: str, standalone=('桌面', '文档', '下载')):
    """「桌面上的报告.pdf」-> (桌面路径, 剩余文件名)。没有目录前缀返回 (None, text)。

    ``standalone`` 决定哪些中文名单独出现时也算目录：说「打开桌面」明显是开文件夹，
    但「打开音乐」更可能是想开网易云音乐，所以「音乐/图片/视频」必须带上
    「里的/上的」或「文件夹」才算目录。
    """
    value = clean_text(text)
    for label, names in COMMON_DIRS:
        for head in (label, '我的' + label):
            for suffix in ('上的', '里的', '中的', '上', '里', '中'):
                if value.startswith(head + suffix):
                    rest = value[len(head + suffix):].strip(' /\\')
                    return _dir_path(names), rest
            if value.startswith(head + '/') or value.startswith(head + '\\'):
                return _dir_path(names), value[len(head) + 1:].strip(' /\\')
            if value == head and (label in standalone or '文件夹' in str(text)
                                  or '目录' in str(text)):
                return _dir_path(names), ''
    return None, value


def _dir_path(names) -> Path:
    home = Path.home()
    for name in names:
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / names[0]


def _expand(text: str) -> Path:
    value = os.path.expandvars(clean_text(text))
    value = os.path.expanduser(value)
    return Path(value)


def _iter_files(root: Path):
    """在常用目录里做有上限的遍历。"""
    root = Path(root)
    if not root.is_dir():
        return
    base_depth = len(root.parts)
    seen = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith('.')]
        if len(Path(current).parts) - base_depth >= _MAX_DEPTH:
            dirs[:] = []
        for name in files:
            seen += 1
            if seen > _MAX_SCAN:
                return
            yield Path(current) / name
        for name in dirs:
            seen += 1
            if seen > _MAX_SCAN:
                return
            yield Path(current) / name


def _norm(text: str) -> str:
    return re.sub(r'[\s\-_·.,，。()（）\[\]【】]', '', str(text)).casefold()


def search(name: str, roots=None):
    """在常用目录里按文件名找（先全等，再包含）。返回 [(path, kind)]。"""
    wanted = _norm(Path(str(name)).stem if Path(str(name)).suffix else name)
    if not wanted:
        return []
    roots = list(roots) if roots else [_dir_path(names) for _label, names in COMMON_DIRS]
    exact, fuzzy = [], []
    for root in roots:
        for path in _iter_files(root):
            candidate = _norm(path.name)
            # 注意要拿「去掉扩展名后的名字」和 wanted 比：文件夹没有扩展名，
            # 用 candidate 比会把所有文件夹都当成命中。
            if candidate == wanted or _norm(Path(path.name).stem) == wanted:
                exact.append(path)
            elif wanted in candidate or candidate in wanted:
                fuzzy.append(path)
            if len(exact) >= _MAX_CANDIDATES:
                return [(p, 'folder' if p.is_dir() else 'file') for p in exact]
    hits = exact or fuzzy[:_MAX_CANDIDATES]
    return [(p, 'folder' if p.is_dir() else 'file') for p in hits]


def resolve(text: str, roots=None) -> dict:
    """把一句话变成真实路径。

    返回 ``{ok, path, kind, candidates, error}``；``error`` 为空表示「不是路径问题」，
    调用方可以据此决定是否交回别的路由。
    """
    raw = clean_text(text)
    if not raw:
        return dict(ok=False, path='', kind='', candidates=[], error='')

    # 1) 完整路径（含盘符、UNC、~/、./）
    candidate = _expand(raw)
    try:
        if candidate.exists():
            return dict(ok=True, path=str(candidate.resolve()),
                        kind='folder' if candidate.is_dir() else 'file',
                        candidates=[], error='')
    except OSError:
        pass
    # 给了明确路径却不存在：直接说没有，别拿去常用目录里瞎猜。
    if _PATH_RE.match(raw) or '\\' in raw or '/' in raw:
        return dict(ok=False, path='', kind='', candidates=[], error='')

    # 2) 「桌面上的报告.pdf」这类带目录的说法
    folder, rest = folder_hint(raw)
    if folder is not None:
        if not rest:
            return dict(ok=True, path=str(folder), kind='folder', candidates=[], error='')
        target = folder / rest
        if target.exists():
            return dict(ok=True, path=str(target),
                        kind='folder' if target.is_dir() else 'file',
                        candidates=[], error='')
        hits = search(rest, [folder])
        if len(hits) == 1:
            return dict(ok=True, path=str(hits[0][0]), kind=hits[0][1],
                        candidates=[], error='')
        if len(hits) > 1:
            return dict(ok=False, path='', kind='', error='',
                        candidates=[dict(path=str(p), name=p.name, kind=k) for p, k in hits])
        return dict(ok=False, path='', kind='', candidates=[], error='')

    # 3) 只给了文件名：去常用目录里找
    if _EXT_RE.search(raw) or len(raw) >= 2:
        hits = search(raw, roots)
        if len(hits) == 1:
            return dict(ok=True, path=str(hits[0][0]), kind=hits[0][1],
                        candidates=[], error='')
        if len(hits) > 1:
            return dict(ok=False, path='', kind='', error='',
                        candidates=[dict(path=str(p), name=p.name, kind=k) for p, k in hits])
    return dict(ok=False, path='', kind='', candidates=[], error='')


def open_target(text: str, roots=None, opener=None) -> dict:
    """打开文件/文件夹。没授权、找不到、启动失败，都如实返回。"""
    if not consent.is_granted():
        return dict(ok=False, code='no_consent', error=consent.NO_PERMISSION_HINT)

    hit = resolve(text, roots)
    if not hit.get('ok'):
        names = [c['name'] for c in hit.get('candidates') or []]
        if names:
            return dict(ok=False, code='ambiguous',
                        error='找到多个可能的目标：%s。请说完整一点（带路径或扩展名）。'
                              % '、'.join(names[:_MAX_CANDIDATES]),
                        candidates=names)
        return dict(ok=False, code='not_found',
                    error='本机没有找到「%s」。可以直接说完整路径，例如「打开 D:\\报告.pdf」。'
                          % clean_text(text))

    path = Path(hit['path'])
    start = opener or os.startfile
    try:
        start(str(path))  # noqa: S606 - 打开文件就是这个模块的职责
    except OSError as exc:
        return dict(ok=False, code='open_failed',
                    error='请 Windows 打开「%s」时失败：%s' % (path.name, exc))
    label = '文件夹' if hit['kind'] == 'folder' else '文件'
    return dict(ok=True, path=str(path), kind=hit['kind'],
                message='已打开%s《%s》。' % (label, path.name))


if __name__ == '__main__':  # pragma: no cover - 手工排查用
    import sys

    if len(sys.argv) > 1:
        print(open_target(' '.join(sys.argv[1:])))
    else:
        print('权限：', consent.status())
        for label, names in COMMON_DIRS:
            folder = _dir_path(names)
            print('  %-4s %s' % (label, folder))
