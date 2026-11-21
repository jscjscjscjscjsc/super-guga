# -*- coding: utf-8 -*-
"""网站与站内搜索：把「在 B 站搜猫」变成一条能直接打开的网址。

为什么插件自己维护一张站点表
--------------------------
宿主 ``core/fast_commands.py`` 里的 ``SITE_ALIASES`` 只负责 **打开首页**（30 个站），
连 「在 B 站搜索猫」 这种句子都不认——会落到智能体，而智能体要调模型；模型没配
（``ai_config.json`` 还是占位符）时就是那句「模型服务域名解析失败」。搜索本质上是
「拼一条 URL 再打开浏览器」，纯本地就能做完，不该经过模型。

所以这里只做三件事，全部离线、可测试：

* ``resolve(name)``：把「小破站 / bilibili / B站官网」折算成一个站点；
* ``search_url(site, query)``：套模板拼出站内搜索结果页；
* ``open_url(url)``：交给系统默认浏览器打开。

站点表里的模板都是各家公开的搜索结果页地址，不登录也能看到结果。
"""
from __future__ import annotations

import os
import re
import urllib.parse
import webbrowser

#: 站点：显示名、首页、站内搜索模板（``{q}`` 会被 URL 编码后的关键词替换）。
SITES = {
    'b站': ('B站', 'https://www.bilibili.com',
            'https://search.bilibili.com/all?keyword={q}'),
    '哔哩哔哩': ('B站', 'https://www.bilibili.com',
                'https://search.bilibili.com/all?keyword={q}'),
    'bilibili': ('B站', 'https://www.bilibili.com',
                 'https://search.bilibili.com/all?keyword={q}'),
    '小破站': ('B站', 'https://www.bilibili.com',
              'https://search.bilibili.com/all?keyword={q}'),
    '知乎': ('知乎', 'https://www.zhihu.com',
             'https://www.zhihu.com/search?type=content&q={q}'),
    '微博': ('微博', 'https://weibo.com', 'https://s.weibo.com/weibo?q={q}'),
    '百度': ('百度', 'https://www.baidu.com', 'https://www.baidu.com/s?wd={q}'),
    '必应': ('必应', 'https://www.bing.com', 'https://www.bing.com/search?q={q}'),
    'bing': ('必应', 'https://www.bing.com', 'https://www.bing.com/search?q={q}'),
    '谷歌': ('谷歌', 'https://www.google.com',
             'https://www.google.com/search?q={q}'),
    'google': ('谷歌', 'https://www.google.com',
               'https://www.google.com/search?q={q}'),
    '搜狗': ('搜狗', 'https://www.sogou.com', 'https://www.sogou.com/web?query={q}'),
    '淘宝': ('淘宝', 'https://www.taobao.com',
             'https://s.taobao.com/search?q={q}'),
    '天猫': ('天猫', 'https://www.tmall.com',
             'https://list.tmall.com/search_product.htm?q={q}'),
    '京东': ('京东', 'https://www.jd.com',
             'https://search.jd.com/Search?keyword={q}'),
    '拼多多': ('拼多多', 'https://www.pinduoduo.com',
               'https://mobile.yangkeduo.com/search_result.html?search_key={q}'),
    '豆瓣': ('豆瓣', 'https://www.douban.com',
             'https://www.douban.com/search?q={q}'),
    '豆瓣电影': ('豆瓣电影', 'https://movie.douban.com',
                 'https://search.douban.com/movie/subject_search?search_text={q}'),
    '抖音': ('抖音', 'https://www.douyin.com',
             'https://www.douyin.com/search/{q}'),
    '快手': ('快手', 'https://www.kuaishou.com',
             'https://www.kuaishou.com/search/video?searchKey={q}'),
    '小红书': ('小红书', 'https://www.xiaohongshu.com',
               'https://www.xiaohongshu.com/search_result?keyword={q}'),
    '贴吧': ('百度贴吧', 'https://tieba.baidu.com',
             'https://tieba.baidu.com/f/search/res?qw={q}'),
    '百度贴吧': ('百度贴吧', 'https://tieba.baidu.com',
                 'https://tieba.baidu.com/f/search/res?qw={q}'),
    '维基': ('维基百科', 'https://zh.wikipedia.org',
             'https://zh.wikipedia.org/w/index.php?search={q}'),
    '维基百科': ('维基百科', 'https://zh.wikipedia.org',
                 'https://zh.wikipedia.org/w/index.php?search={q}'),
    'youtube': ('YouTube', 'https://www.youtube.com',
                'https://www.youtube.com/results?search_query={q}'),
    '油管': ('YouTube', 'https://www.youtube.com',
             'https://www.youtube.com/results?search_query={q}'),
    'github': ('GitHub', 'https://github.com',
               'https://github.com/search?q={q}'),
    'csdn': ('CSDN', 'https://www.csdn.net',
             'https://so.csdn.net/so/search?q={q}'),
    '掘金': ('掘金', 'https://juejin.cn', 'https://juejin.cn/search?query={q}'),
    'pypi': ('PyPI', 'https://pypi.org', 'https://pypi.org/search/?q={q}'),
    'npm': ('npm', 'https://www.npmjs.com',
            'https://www.npmjs.com/search?q={q}'),
    'stackoverflow': ('Stack Overflow', 'https://stackoverflow.com',
                      'https://stackoverflow.com/search?q={q}'),
    '知网': ('中国知网', 'https://www.cnki.net',
             'https://kns.cnki.net/kns8s/defaultresult/index?kw={q}'),
    '知乎专栏': ('知乎', 'https://zhuanlan.zhihu.com',
                 'https://www.zhihu.com/search?type=content&q={q}'),
    '网易云音乐': ('网易云音乐', 'https://music.163.com',
                   'https://music.163.com/#/search/m/?s={q}&type=1'),
    '网易云': ('网易云音乐', 'https://music.163.com',
               'https://music.163.com/#/search/m/?s={q}&type=1'),
    '云音乐': ('网易云音乐', 'https://music.163.com',
               'https://music.163.com/#/search/m/?s={q}&type=1'),
    'qq音乐': ('QQ音乐', 'https://y.qq.com',
               'https://y.qq.com/n/ryqq/search?w={q}'),
    'qq音乐网': ('QQ音乐', 'https://y.qq.com',
                 'https://y.qq.com/n/ryqq/search?w={q}'),
    '酷狗': ('酷狗音乐', 'https://www.kugou.com',
             'https://www.kugou.com/yy/html/search.html#searchType=song&searchKeyWord={q}'),
    '酷我': ('酷我音乐', 'https://www.kuwo.cn',
             'https://www.kuwo.cn/search/list?key={q}'),
    '喜马拉雅': ('喜马拉雅', 'https://www.ximalaya.com',
                 'https://www.ximalaya.com/search/{q}'),
}

#: 听起来像站点、但其实是站内搜索之外的短名（避免「在 B 站」以外的误判）。
_MUSIC_SITES = frozenset(('网易云音乐', 'QQ音乐', '酷狗音乐', '酷我音乐',
                          '喜马拉雅'))

#: 站点名后面常见的余缀，先剥掉再查表。
_SUFFIX = re.compile(r'(?:官网|网站|网页|主页|首页)$')
#: 搜索动词：站内搜索允许的几种说法。
VERBS = ('搜索', '搜一下', '搜一搜', '搜', '查找', '查一下', '查查', '查',
         '找一下', '找找', '找')


def _key(name: str) -> str:
    value = str(name or '').strip().casefold().replace(' ', '')
    return _SUFFIX.sub('', value)


def resolve(name: str):
    """站点名 -> ``dict(key/name/home/search/music)``；认不出返回 None。"""
    key = _key(name)
    if not key:
        return None
    entry = SITES.get(key)
    if entry is None:
        # 「B站官网」「网易云音乐网站」这类：剥了余缀仍不中，再试一次原样。
        entry = SITES.get(str(name or '').strip().casefold().replace(' ', ''))
    if entry is None:
        return None
    display, home, search = entry
    return dict(key=key, name=display, home=home, search=search,
                music=display in _MUSIC_SITES)


def all_names() -> list:
    """给正则用的站点名清单（长的在前，避免「豆瓣电影」被「豆瓣」截断）。"""
    return sorted(SITES.keys(), key=len, reverse=True)


def search_url(site, query: str) -> str:
    """拼站内搜索地址；认不出站点或没关键词就返回空串。"""
    hit = resolve(site)
    text = str(query or '').strip()
    if not hit or not text:
        return ''
    return hit['search'].format(q=urllib.parse.quote(text))


def engine_url(query: str, engine: str = '必应') -> str:
    """通用搜索：默认必应（不用梯子也能开）。"""
    return search_url(engine, query) or search_url('必应', query)


def is_domain(text: str) -> bool:
    """像不像一个域名（``example.com``、``foo.cn/a``）。"""
    value = str(text or '').strip()
    if ' ' in value or len(value) > 120:
        return False
    return bool(re.fullmatch(r'(?:https?://)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+'
                             r'(?::\d+)?(?:/[^\u4e00-\u9fff\s]*)?', value))


def normalize_url(text: str) -> str:
    value = str(text or '').strip()
    if not value:
        return ''
    return value if re.match(r'https?://', value, re.I) else 'https://' + value


def open_url(url: str) -> dict:
    """用系统默认浏览器打开。返回 ``{ok, url, message/error}``。"""
    target = str(url or '').strip()
    if not target:
        return dict(ok=False, code='empty_url', error='没有要打开的网址。')
    try:
        if os.name == 'nt' and hasattr(os, 'startfile'):
            os.startfile(target)  # noqa: S606 - 只打开浏览器，不执行文件
        else:  # pragma: no cover - 非 Windows 只在开发机上用到
            if not webbrowser.open_new_tab(target):
                raise OSError('系统没有可用的默认浏览器')
    except Exception as exc:  # noqa: BLE001 - 打不开要如实说，别抛给宿主
        return dict(ok=False, code='open_failed', url=target,
                    error='没能打开浏览器：%s: %s' % (type(exc).__name__, exc))
    return dict(ok=True, url=target, message='已经在浏览器打开这个网址。')


def open_search(site, query: str) -> dict:
    hit = resolve(site)
    url = search_url(site, query)
    if not url:
        return dict(ok=False, code='unknown_site',
                    error='没认出这个搜索平台：%s' % (site or ''))
    result = open_url(url)
    result.update(site=hit['name'], query=str(query or '').strip(),
                  message=('已在%s搜索「%s」。' % (hit['name'], str(query or '').strip())
                           if result.get('ok') else ''))
    return result


def open_site(name: str = '') -> dict:
    """打开网站。

    * 给了已知站点 -> 直接开首页；
    * 给了域名 -> 补 ``https://`` 打开；
    * 没给 / 认不出 -> 开浏览器，并在回执里如实说明做了什么。
    """
    value = str(name or '').strip()
    if not value:
        result = open_url('https://www.bing.com')
        result['message'] = '已打开浏览器。想搜什么直接说「在 B 站搜猫」或「搜一下天气」。'
        return result
    hit = resolve(value)
    if hit:
        result = open_url(hit['home'])
        result.update(site=hit['name'],
                      message='已打开%s。' % hit['name'] if result.get('ok') else '')
        return result
    if is_domain(value):
        url = normalize_url(value)
        result = open_url(url)
        result.update(site='', url=url,
                      message='已打开 %s。' % url if result.get('ok') else '')
        return result
    # 认不出的名字：别装懂，给它搜一下，用户一眼就能点到。
    url = engine_url(value)
    result = open_url(url)
    result.update(site='', searched=True, url=url,
                  message='没认出「%s」是哪个网站，已经在必应搜出来，点第一条就行。'
                          % value if result.get('ok') else '')
    return result


if __name__ == '__main__':  # 不启动桌宠也能看：不加参数只打表，加参数才试一句
    import sys
    from pathlib import Path
    if not sys.argv[1:]:
        seen = {}
        for display, _home, search in SITES.values():
            seen.setdefault(display, search)
        print('支持的平台（%d 个）：' % len(seen))
        for display in seen:
            print('  ·', display)
        print('\n示例：python plugins/guga_commander/web_lib.py 在B站搜猫')
        raise SystemExit(0)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from plugins.guga_commander import commander
    text = ' '.join(sys.argv[1:])
    cmd = commander.parse(text)
    print('原话：', text)
    print('解析：', cmd.as_dict())
    if cmd.action in ('site_search', 'open_site', 'music_search', 'music_like'):
        print('结果：', commander.run(cmd).get('message'))
    else:
        print('这句话不归搜索管，没有打开浏览器。')
