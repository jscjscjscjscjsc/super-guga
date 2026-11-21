# -*- coding: utf-8 -*-
"""网易云音乐**网页版**：查歌、开搜索页、在浏览器里点 ♥。

> 现在这是**退路**。日常操作走 ``cloudmusic_lib``（直接驱动客户端），
> 只有客户端没装、或界面认不出来时才退到这里。

两层能力，可信度完全不同，所以分开做、也分开说：

* **搜索 / 打开**（``search`` / ``open_search``）——查公开搜索接口拿歌名和 id，
  拼出网页地址交给浏览器。离线可测、可重复，不会误点。
* **收藏**（``like_song``）——网页版没有「加喜欢」的地址，只能真的去点一下那个
  ♥。这里复用宿主自己的浏览器控件（``core/existing_browser``，UIA + 快照 ref），
  点完再观察一次按钮有没有变成「取消喜欢」；**没验证到就绝不报成功**，
  而是把页面留在那儿，告诉用户手动点一下。宁可不体面，也不谎报。

收藏必须在「本机文件权限 / 完整访问」打开时才能做（宿主对浏览器操作的硬性要求），
没开就直接拒绝，一个控件都不碰。
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
import uuid

try:
    from . import consent
except ImportError:  # 单文件调试
    import consent

SEARCH_PAGE = 'https://music.163.com/#/search/m/?s={q}&type=1'
SONG_PAGE = 'https://music.163.com/#/song?id={song_id}'
SEARCH_API = ('https://music.163.com/api/search/get/web?s={q}&type=1&limit={limit}')
_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'),
    'Referer': 'https://music.163.com/',
}

#: 喜欢按钮可能的各种叫法；判断时先排除「取消喜欢」。
_LIKE_LABELS = ('喜欢', '添加到我喜欢', '收藏', '我喜欢')
_LIKED_LABELS = ('取消喜欢', '已喜欢', '已收藏')


def _quote(text: str) -> str:
    return urllib.parse.quote(str(text or '').strip())


def search(query: str, limit: int = 5) -> dict:
    """查网易云公开搜索接口（不需要登录）。返回 ``{ok, songs, error}``。"""
    text = str(query or '').strip()
    if not text:
        return dict(ok=False, code='empty_query', error='要搜什么歌？', songs=[])
    url = SEARCH_API.format(q=_quote(text), limit=max(1, min(20, int(limit))))
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode('utf-8', 'replace'))
    except Exception as exc:  # noqa: BLE001 - 断网/被墙都要能体面收场
        return dict(ok=False, code='search_failed', songs=[],
                    error='没连上网易云搜索：%s: %s' % (type(exc).__name__, exc))
    songs = []
    for item in ((payload.get('result') or {}).get('songs') or [])[:limit]:
        if not isinstance(item, dict) or not item.get('id'):
            continue
        artists = '、'.join(str(a.get('name') or '') for a in (item.get('artists') or [])
                            if isinstance(a, dict))
        songs.append(dict(id=int(item['id']), name=str(item.get('name') or ''),
                          artists=artists,
                          album=str((item.get('album') or {}).get('name') or ''),
                          duration=int(item.get('duration') or 0)))
    if not songs:
        return dict(ok=False, code='not_found', songs=[],
                    error='网易云没搜到《%s》。' % text)
    return dict(ok=True, songs=songs, query=text,
                message='搜到 %d 首，第一首是《%s》%s。'
                        % (len(songs), songs[0]['name'],
                           (' · ' + songs[0]['artists']) if songs[0]['artists'] else ''))


def _norm_name(text: str) -> str:
    value = str(text or '').strip().casefold()
    for pair in ('（）', '()', '【】', '[]', '  '):
        value = value.replace(pair[0], '').replace(pair[1], '')
    return value.replace(' ', '')


def pick(query: str) -> dict:
    """在搜索结果里挑一首：同名优先，否则取第一条。返回 ``{ok, song}``。"""
    found = search(query, limit=8)
    songs = found.get('songs') or []
    if not songs:
        return dict(ok=False, code=found.get('code') or 'not_found',
                    error=found.get('error') or '没搜到这首歌。', songs=[])
    wanted = _norm_name(query)
    for song in songs:
        if _norm_name(song['name']) == wanted:
            return dict(ok=True, song=song, songs=songs, exact=True)
    return dict(ok=True, song=songs[0], songs=songs, exact=False)


def song_url(song_id: int) -> str:
    return SONG_PAGE.format(song_id=int(song_id))


def open_search(query: str) -> dict:
    """打开网页版搜索页（不登录也能看结果），顺带报一下第一首是什么。"""
    from . import web_lib
    text = str(query or '').strip()
    if not text:
        return dict(ok=False, code='empty_query', error='要搜什么歌？')
    result = web_lib.open_url(SEARCH_PAGE.format(q=_quote(text)))
    found = search(text)  # 断网也不影响已经把页面打开这件事
    songs = (found.get('songs') or [])[:5]
    tail = ''
    if songs:
        first = songs[0]
        tail = '，第一首是《%s》%s' % (first['name'],
                                      (' · ' + first['artists']) if first['artists'] else '')
    result.update(query=text, songs=songs,
                  message=('已在网易云音乐搜「%s」%s。' % (text, tail)
                           if result.get('ok') else ''))
    return result


# ---------------------------------------------------------------------------
# 收藏：浏览器里真的点一下 ♥
# ---------------------------------------------------------------------------

def _session():
    """复用宿主的浏览器控制器（沿用用户已登录的浏览器与配置）。"""
    from core.computer_control import ComputerControl
    from core.existing_browser import ExistingBrowser
    return ExistingBrowser(ComputerControl()), threading.Event()


def _labels(controls) -> list:
    return [str(c.get('label') or '').strip() for c in controls or []
            if isinstance(c, dict)]


def find_like_control(controls) -> dict:
    """在快照里找「喜欢」按钮；已经喜欢过或找不到都返回空 dict。"""
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        label = str(control.get('label') or '').strip()
        if not label:
            continue
        if any(word in label for word in _LIKED_LABELS):
            continue  # 已经是「取消喜欢」：不用再点
        if control.get('type') not in ('Button', 'CheckBox', 'Hyperlink',
                                       'MenuItem', None):
            continue
        if label in _LIKE_LABELS:
            return control
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        label = str(control.get('label') or '').strip()
        if '喜欢' in label and not any(word in label for word in _LIKED_LABELS):
            if control.get('type') in ('Button', 'CheckBox', 'Hyperlink',
                                       'MenuItem'):
                return control
    return {}


def already_liked(controls) -> bool:
    return any(word in label for label in _labels(controls)
               for word in _LIKED_LABELS)


#: 观察重试：页面要加载，一次快照常常还看不到 ♥。测试里会调成 0 秒。
RETRY_TRIES = 4
RETRY_WAIT = 1.4


def _snapshot_until(session, cancel, want, tries: int = None, wait: float = None):
    """反复观察，直到 ``want(controls)`` 为真；返回最后一次快照。"""
    tries = RETRY_TRIES if tries is None else tries
    wait = RETRY_WAIT if wait is None else wait
    snapshot = {}
    for attempt in range(tries):
        snapshot = session.snapshot(cancel)
        if want(snapshot.get('controls') or []):
            return snapshot
        if attempt + 1 < tries and wait:
            cancel.wait(wait)
    return snapshot or session.snapshot(cancel)


def _mouse_click(session, ref, cancel) -> dict:
    """退路：有些网页元素没有 Invoke 模式，只能按坐标点一下。"""
    try:
        computer = getattr(session, 'computer', None)
        hwnd = (getattr(session, 'bound', None) or {}).get('hwnd')
        if computer is None or not hwnd or not ref:
            return dict(ok=False, error='拿不到浏览器窗口，没法补点')
        return computer.execute(dict(action='uia', hwnd=hwnd, uia_ref=ref,
                                     uia_operation='click'), cancel)
    except Exception as exc:  # noqa: BLE001
        return dict(ok=False, error='%s: %s' % (type(exc).__name__, exc))


def like_song(query: str) -> dict:
    """把一首歌加进「我喜欢的音乐」。

    返回 ``{ok, verified, song, steps, message/error}``。``ok`` 只在**观察到了**
    按钮变成「取消喜欢」时才为真。
    """
    text = str(query or '').strip()
    if not text:
        return dict(ok=False, code='need_song',
                    error='要收藏哪一首？说「把晴天加入我喜欢」这样带歌名的。')
    if not consent.is_granted():
        return dict(ok=False, code='no_consent',
                    error='收藏要在浏览器里点一下 ♥，需要先开启「完整访问」。\n'
                          '开启方式：点对话窗口右上角「···」→「软件权限…」，'
                          '勾选「允许咕嘎完整访问本机」。')

    chosen = pick(text)
    if not chosen.get('ok'):
        fallback = open_search(text)
        return dict(ok=False, code=chosen.get('code') or 'not_found',
                    songs=chosen.get('songs') or [], fallback=fallback,
                    error=(chosen.get('error') or '没搜到这首歌。')
                          + ('已经把搜索页打开，点一首手动收藏就行。'
                             if fallback.get('ok') else ''))
    song = chosen['song']
    url = song_url(song['id'])
    steps = []

    try:
        session, cancel = _session()
    except Exception as exc:  # noqa: BLE001
        return dict(ok=False, code='no_browser', song=song,
                    error='拿不到浏览器控件：%s: %s' % (type(exc).__name__, exc))

    try:
        nav = session.navigate(url, cancel)
        steps.append(dict(step='打开歌曲页', ok=bool(nav.get('ok')),
                          detail=nav.get('message') or nav.get('error') or ''))
        if not nav.get('ok'):
            return dict(ok=False, code='navigate_failed', song=song, url=url,
                        steps=steps,
                        error='没能打开歌曲页：%s' % str(nav.get('error')
                                                        or '浏览器没有响应')[:160])

        snapshot = _snapshot_until(session, cancel,
                                   lambda controls: bool(find_like_control(controls))
                                   or already_liked(controls))
        controls = snapshot.get('controls') or []
        steps.append(dict(step='找到喜欢按钮',
                          ok=bool(find_like_control(controls)) or already_liked(controls),
                          detail='共看到 %d 个可点控件' % len(controls)))

        if already_liked(controls):
            return dict(ok=True, verified=True, already=True, song=song, url=url,
                        steps=steps,
                        message='《%s》%s 已经在「我喜欢的音乐」里了。'
                                % (song['name'], song['artists']))

        target = find_like_control(controls)
        if not target:
            return dict(ok=False, code='no_like_button', song=song, url=url,
                        steps=steps, action_sent=False,
                        error='没在页面上找到「喜欢」按钮（可能还没登录，或页面结构变了）。\n'
                              '歌曲页已经打开，点一下那个 ♥ 就行。')

        try:
            clicked = session.execute(dict(
                action='click', ref=target.get('ref'),
                operation_id=uuid.uuid4().hex,
                page_id=snapshot.get('page_id'),
                snapshot_id=snapshot.get('snapshot_id')), cancel)
        except Exception as exc:  # noqa: BLE001 - 点不动就退到坐标点击
            clicked = dict(ok=False, error='%s: %s' % (type(exc).__name__, exc))
        if not clicked.get('ok'):
            fallback = _mouse_click(session, target.get('ref'), cancel)
            steps.append(dict(step='改按坐标点击', ok=bool(fallback.get('ok')),
                              detail=str(fallback.get('error') or '已在控件中心点一下')[:160]))
            if fallback.get('ok'):
                clicked = dict(fallback, ok=True, fallback_click=True)
        steps.append(dict(step='点击喜欢', ok=bool(clicked.get('ok')),
                          detail=str(clicked.get('error') or '已发送点击')[:160]))
        if not clicked.get('ok'):
            return dict(ok=False, code='click_failed', song=song, url=url,
                        steps=steps, action_sent=bool(clicked.get('action_sent')),
                        error='点击没发出去：%s' % str(clicked.get('error')
                                                     or '浏览器拒绝了这次点击')[:160])

        after = _snapshot_until(session, cancel, already_liked)
        verified = already_liked(after.get('controls') or [])
        steps.append(dict(step='确认已收藏', ok=verified,
                          detail='按钮已变成「取消喜欢」' if verified
                          else '没观察到状态变化'))
        if verified:
            return dict(ok=True, verified=True, song=song, url=url, steps=steps,
                        message='已把《%s》%s 加进「我喜欢的音乐」。'
                                % (song['name'], song['artists']))
        return dict(ok=False, code='unverified', song=song, url=url, steps=steps,
                    action_sent=True,
                    error='点了 ♥，但没观察到收藏成功的状态（可能没登录）。\n'
                          '歌曲页还开着，麻烦你看一眼确认一下。')
    except Exception as exc:  # noqa: BLE001 - 浏览器/控件层出错也要说人话
        return dict(ok=False, code='browser_error', song=song, url=url, steps=steps,
                    error='浏览器操作没做完：%s: %s'
                          % (type(exc).__name__, str(exc)[:160]))
    finally:
        try:
            session.execute(dict(action='session_stop'), cancel)
        except Exception:  # noqa: BLE001 - 解绑失败不影响结果
            pass


if __name__ == '__main__':  # 只读验证：python music_lib.py "孤勇者"
    import sys
    words = sys.argv[1:]
    if not words:
        print('用法：python plugins/guga_commander/music_lib.py "孤勇者"')
        print('（只查公开搜索接口，不会打开浏览器、不会收藏）')
        raise SystemExit(0)
    found = search(' '.join(words))
    if not found.get('ok'):
        print('没查到：', found.get('error'))
        raise SystemExit(1)
    for song in found['songs']:
        print('%s  %s  %s' % (song['id'], song['name'], song['artists']))
