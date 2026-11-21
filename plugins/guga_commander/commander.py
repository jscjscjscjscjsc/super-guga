# -*- coding: utf-8 -*-
"""咕嘎指令台 · 插件核心。

把一句中文口语变成一次受控的桌面动作：

    打开微信            -> desktop_command(open, 微信)
    打开 steam          -> steam://open/main
    打开 steam 上的巫师3 -> steam://rungameid/292030（AppID 从本机 manifest 读）
    自己打一局战棋      -> battleground(auto, 120 步 / 1200 秒)
    看看打到哪了        -> battleground(status)
    停一下              -> battleground(stop)

设计原则（改这个文件时请保留）：

1. **纯标准库**：解析层不 import PyQt、不 import 项目模块，所以可以单文件运行、
   可以单元测试，也不会把桌宠启动拖慢。
2. **执行器注入**：真正干活靠 ``call(tool, args)`` 回调。宿主把 AppRuntime.execute
   注入进来；测试里注入假执行器。解析层永远不知道 Qt 的存在。
3. **只复用、不重造**：打开应用走项目已有的 ``desktop_command``；Steam 游戏没有
   可以直接点的 exe，只能走系统注册的 ``steam://rungameid`` 协议，AppID 从本机
   manifest 读取（见 ``steam_lib``），同样不模拟键鼠、不改注册表。
4. **不吹牛**：项目目前只有「炉石酒馆战棋 · 招募阶段有限接管」这一种自动玩法。
   说别的游戏时如实告知，绝不假装能代打。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Callable, Optional

try:
    from . import (app_lib, cloudmusic_lib, consent, files_lib, music_lib,
                   steam_lib, web_lib)
except ImportError:  # 直接 `python commander.py` 跑演示时没有包上下文
    import app_lib
    import cloudmusic_lib
    import consent
    import files_lib
    import music_lib
    import steam_lib
    import web_lib

PLUGIN_ID = 'guga-commander'
VERSION = '1.4.0'

# ---------------------------------------------------------------------------
# 可调参数：觉得别名不够用，直接往这两张表里加就行
# ---------------------------------------------------------------------------

#: 口语说法 -> 交给宿主 ``desktop_command`` / ``fast_commands`` 的软件名。
#: 键统一小写；未列出的名字会原样传给宿主（宿主自己会查已安装软件）。
APP_ALIASES = {
    '微信': '微信', 'wechat': '微信', 'wx': '微信',
    'qq': 'QQ', '腾讯qq': 'QQ',
    'vscode': 'Visual Studio Code', 'vs code': 'Visual Studio Code',
    'vs': 'Visual Studio Code', '代码编辑器': 'Visual Studio Code',
    'pycharm': 'PyCharm', 'python编辑器': 'PyCharm',
    '记事本': '记事本', 'notepad': '记事本',
    '计算器': '计算器', '画图': '画图',
    '资源管理器': '资源管理器', '我的电脑': '资源管理器',
    '浏览器': '浏览器', 'edge': '浏览器', 'chrome': '浏览器',
    'b站': 'b站', '哔哩哔哩': 'b站', 'bilibili': 'b站',
    '百度': '百度', '知乎': '知乎', 'github': 'github',
    '终端': '终端', 'cmd': '命令提示符', '命令行': '命令提示符',
    '任务管理器': '任务管理器', '设置': '设置',
    '网易云': '网易云音乐', '网易云音乐': '网易云音乐',
    '腾讯会议': '腾讯会议', '会议': '腾讯会议',
    '炉石': '炉石传说', '炉石传说': '炉石传说', 'hearthstone': '炉石传说',
    '战网': '战网', 'battlenet': '战网',
    'steam': 'Steam',
}

#: 游戏口语 -> 游戏标识。项目当前只有酒馆战棋具备自动操作后端。
GAME_ALIASES = {
    '炉石传说': 'battleground', '炉石': 'battleground',
    '酒馆战棋': 'battleground', '战棋': 'battleground',
    'hearthstone': 'battleground', 'battlegrounds': 'battleground',
}

#: 能被自动操作的游戏（与 ``core/bg_director.py`` 的实际能力保持一致）。
AUTO_PLAYABLE = frozenset(('battleground',))

#: 其他游戏被点到时，用来提醒「只能帮忙打开」。
OTHER_GAME_WORDS = (
    '原神', '英雄联盟', 'lol', '王者荣耀', '和平精英', 'csgo', 'cs2', 'dota',
    '我的世界', 'minecraft', '塞尔达', '黑神话', '艾尔登法环', '星露谷',
    '三国杀', '金铲铲', '云顶之弈', '麻将', '斗地主', '扫雷', '纸牌',
)

# 安全预算：与 BattlegroundDirector.start() 的校验范围一致，别调高。
MAX_ACTIONS_CEILING = 500
SECONDS_CEILING = 1800
DEFAULT_MAX_ACTIONS = 120
DEFAULT_SECONDS = 1200

# ---------------------------------------------------------------------------
# 命令对象
# ---------------------------------------------------------------------------

ACTIONS = (
    'open_app', 'open_local_app', 'open_file', 'open_steam', 'open_steam_game',
    'list_steam_games', 'file_access_on', 'file_access_off',
    'site_search', 'open_site', 'music_search', 'music_like', 'music_play',
    'auto_play', 'bg_step', 'bg_observe', 'bg_status', 'bg_stop',
    'list_apps', 'unsupported_game', 'help', 'unknown',
)


@dataclass
class Command:
    """解析结果。``action`` 见 ACTIONS；``note`` 是要如实告诉用户的补充说明。"""
    action: str = 'unknown'
    target: str = ''
    site: str = ''
    game: str = ''
    appid: int = 0
    max_actions: int = DEFAULT_MAX_ACTIONS
    seconds: int = DEFAULT_SECONDS
    raw: str = ''
    note: str = ''

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Command':
        """从 hook 传过来的字典还原，容忍缺字段 / 多字段。"""
        keys = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in dict(data or {}).items() if k in keys})


# ---------------------------------------------------------------------------
# 文本解析
# ---------------------------------------------------------------------------

# 停止类短句：整句就是一个明确的「叫停」，不与「打开…然后停下」这类复合句混淆。
_STOP_RE = re.compile(
    r'^(?:停一下|停下|停止|别玩了|不要玩了|别打了|不要打了|住手|收手|'
    r'取消自动|暂停自动|别再玩了|别自己打了|别自己玩了|'
    r'停止自动|停止战棋|停止接管|停止后台任务)'
    r'(?:自动|战棋|对战|接管|游戏|任务)?(?:吧|啦|了|呢)?$'
)
# 「别再自动打了」这类带上下文的叫停
_STOP_CONTEXT_RE = re.compile(
    r'^(?:停|别|不要|取消|暂停).{0,10}(?:自动|战棋|对战|代打|接管|后台|游戏|玩|打)'
)
_STATUS_RE = re.compile(
    r'(?:战棋|对战|游戏|自动|后台|打到|进行|打得).{0,8}'
    r'(?:状态|进度|怎样|怎么样|如何|到哪|哪了|完了没|结束没)'
    r'|(?:看看|查一下|报一下).{0,6}(?:状态|进度|战况)'
)
_OBSERVE_RE = re.compile(r'观战|只看别动|看一局|自己看看|只观察|旁观')
_STEP_RE = re.compile(r'走一步|下一步|操作一步|打一步|来一步|试一步|单步')
_AUTO_RE = re.compile(
    r'自己玩|自己打|帮我玩|帮我打|替我玩|替我打|代打|你来打|你上|'
    r'自动(?:对战|打|玩|接管|操作)|托管|开始自动|交给?你玩'
)
_LIST_RE = re.compile(r'有哪些(?:软件|应用|应用可以开)|能打开什么|支持哪些|列出(?:应用|软件)|可用软件|软件列表')
_HELP_RE = re.compile(r'怎么用|帮助|指令(?:有|说明|列表)?|能干什么|会什么|功能说明')

# 文件权限开关。只在整句就是「允许/关闭 + 文件权限」时触发，
# 「打开文件 D:\x.pdf」这类带路径的句子不会被误判。
_FILE_ON_RE = re.compile(
    r'^(?:请|帮我|麻烦)?\s*(?:允许|授权|开启|同意)\s*(?:咕嘎)?\s*(?:读取|访问|操作|使用)?\s*'
    r'(?:本机|本地|电脑)?\s*(?:上)?(?:的)?\s*文件(?:权限|访问权限|访问)?\s*(?:吧|呀|呢|啊)?$'
    r'|^(?:请|帮我|麻烦)?\s*打开\s*(?:咕嘎)?\s*(?:本机|本地|电脑)\s*(?:上)?(?:的)?\s*文件'
    r'(?:权限|访问权限|访问)?\s*(?:吧|呀|呢|啊)?$'
)
_FILE_OFF_RE = re.compile(
    r'^(?:请|帮我|麻烦)?\s*(?:关闭|禁止|取消|收回|关掉|禁用)\s*(?:咕嘎)?\s*'
    r'(?:本机|本地|电脑)?\s*(?:上)?(?:的)?\s*文件(?:权限|访问权限|访问)?\s*(?:吧|呀|呢|啊)?$'
)
# 文件句里的「复合句」判断要比软件名严格得多：这里的「下载」是目录名，
# 不能像 _COMPOUND_RE 那样把「打开下载里的安装包」当成两件事。
_FILE_COMPOUND_RE = re.compile(
    r'然后|接着|并且|再帮|之后|以后|搜索|查找|发送|点击|登录|新建|创建|修改|编辑|删除|'
    r'[，,。;；]'
)

# ---------------------------------------------------------------------------
# Steam：客户端本身 / 库里的游戏
# ---------------------------------------------------------------------------

#: 明确不属于 Steam 的目标：已知桌面软件（微信、记事本…）+ 走战网的炉石等。
_DESKTOP_ONLY = frozenset(value.casefold() for value in APP_ALIASES.values()
                          if value.casefold() != 'steam')
_NON_STEAM_GAME_WORDS = frozenset(word.casefold() for word in GAME_ALIASES)

_STEAM_WORDS = r'(?:steam|蒸汽平台|斯蒂姆|斯缇姆)'
_STEAM_CLIENT_RE = re.compile(
    r'^(?:打开|启动|开一下|开|拉起|运行)?\s*' + _STEAM_WORDS + r'\s*'
    r'(?:客户端|平台|主界面|首页|吧|呀|呢|啊|啦|了)?$', re.I)
_STEAM_LIST_RE = re.compile(
    _STEAM_WORDS + r'.{0,8}(?:有(?:哪些|什么|啥)游戏|游戏列表|游戏库|库里)|'
    r'有(?:哪些|什么|啥)游戏|我的游戏库|游戏库里有', re.I)
_STEAM_GAME_PREFIX_RE = re.compile(
    r'^(?:打开|启动|开一下|开|运行|玩一下|玩玩|玩|开一局|来一局|玩一局)\s*'
    r'(?:游戏)?\s*' + _STEAM_WORDS + r'\s*(?:上|里|中)?(?:的)?\s*(.+)$', re.I)
_STEAM_GAME_IN_RE = re.compile(
    r'^(?:在)?\s*' + _STEAM_WORDS + r'\s*(?:上|里|中)\s*(?:玩|打开|启动|开|找)\s*(.+)$', re.I)
_OPEN_GAME_RE = re.compile(r'^(?:打开|启动|开一下|开|运行)\s*游戏\s*[，,:：]?\s*(.+)$')
_PLAY_GAME_RE = re.compile(r'^(?:玩一下|玩玩|玩|开一局|来一局|玩一局)\s*(.+)$')

# 「打开 / 启动 / 拉起 X」「把 X 打开」「X 开一下」
_OPEN_PREFIX_RE = re.compile(r'^(?:打开|启动|拉起|运行|开启|开一下|开个|开)\s*(.+)$')
_OPEN_INVERTED_RE = re.compile(r'^把\s*(.+?)\s*(?:打开|启动|开一下|开起来|拉起)$')
_OPEN_SUFFIX_RE = re.compile(r'^(.+?)\s*(?:打开|启动一下|开一下|开起来|拉起来)$')
# 「看看桌面上的照片.png」——用户想看某个文件，也是打开它
_SEE_FILE_RE = re.compile(r'^(?:看看|看一下|瞧瞧|瞅瞅|打开看看)\s*(.+)$')

# 整句末尾的语气词：只剥语气词和标点，绝不剥「一下」——它可能是「开一下」这个动词的一半。
_SENTENCE_TAIL_RE = re.compile(r'[吧呀呢啊啦嘛哦呗，,。.!！?？\s]+$')
# 软件名末尾的噪声：这里才允许剥「一下 / 给我 / 帮我」。
_TAIL_NOISE_RE = re.compile(
    r'(?:吧|呀|呢|啊|啦|嘛|哦|呗|一下|好不好|可以吗|行吗|谢谢|快点|马上|'
    r'给我|帮我|好不好呀|[，,。.!！?？\s])+$'
)
# 复合句（「打开微信然后发消息」）不该被当成单纯启动
_COMPOUND_RE = re.compile(r'然后|接着|并且|再帮|之后|以后|输入|搜索|发送|点击|登录|下载|安装')


# ---------------------------------------------------------------------------
# 网站 / 站内搜索 / 音乐
# ---------------------------------------------------------------------------

_SITE_ALT = '|'.join(re.escape(name) for name in web_lib.all_names())
_SEARCH_VERB = r'(?:搜索|搜一下|搜一搜|搜|查找|查一下|查查|查|找一下|找找|找)'
# 「在 B 站搜猫」「用必应查天气」「网易云音乐搜晴天」
_SITE_SEARCH_RE = re.compile(
    r'^(?P<site>' + _SITE_ALT + r')\s*(?:上面|上|里|中|里面)?\s*'
    r'(?:帮我|给我|请)?\s*' + _SEARCH_VERB + r'\s*(?P<q>.+?)\s*$', re.I)
# 句子开头那些不影响语义的前缀，先剥掉再认站点（「打开B站搜索猫」也要能认）
_SEARCH_PREFIX_RE = re.compile(r'^(?:(?:打开|在|用|到|去|上|帮我|给我|请|麻烦)\s*)+')

# 打开网站：没给名字（「打开一个网站」）或名字带「网站/官网」后缀
_OPEN_SITE_ANY_RE = re.compile(
    r'^(?:帮我|给我|请)?\s*(?:打开|访问|前往|进)\s*(?:一下)?\s*'
    r'(?:一个|个|随便一个)?\s*(?:网站|网页|网址)\s*(?:吧|呀|呗)?$')
_OPEN_SITE_NAMED_RE = re.compile(
    r'^(?:帮我|给我|请)?\s*(?:打开|访问|前往|进)\s*(?:一下)?\s*(?:一个|个)?\s*'
    r'(?P<name>.+?)\s*(?:这个|那个)?\s*(?:网站|网页|官网|网址)\s*(?:吧|呀|呗)?$')

# 音乐收藏：三种说法
_LIKE_TARGET = (r'(?:我的?喜欢(?:的音乐|列表|歌单)?|我喜欢的音乐|喜欢的音乐|'
                r'喜欢列表|喜欢歌单|红心|收藏夹|收藏列表)')
_LIKE_VERB = (r'(?:加入|加到|添加进|添加到|添加|放进|放到|存进|收进|收藏到|'
              r'收藏进|加进)')
_MUSIC_LIKE_PUT_RE = re.compile(      # 把晴天加入我喜欢
    r'^(?:把|将)\s*(?P<song>.+?)\s*(?:这首|这首歌|这个|首|个)?\s*(?:歌曲|歌|音乐)?\s*'
    + _LIKE_VERB + r'\s*' + _LIKE_TARGET + r'(?:里|中|里面|上)?\s*(?:吧|呀|呗)?$',
    re.I)
_MUSIC_LIKE_ADD_RE = re.compile(      # 添加一首音乐进我的喜欢（可能没歌名）
    r'^(?:添加|添加进|加|加进|放|放进|存|收藏)\s*(?:一首|首|个)?\s*'
    r'(?:歌曲|歌|音乐)?\s*(?:进|到|入|去)\s*' + _LIKE_TARGET +
    r'(?:里|中|里面)?\s*(?:吧|呀|呗)?$', re.I)
_MUSIC_LIKE_VERB_RE = re.compile(     # 收藏 晴天 / 喜欢一下晴天
    r'^(?:收藏|喜欢|红心)\s*(?:一下)?\s*(?P<song>.+?)\s*(?:这首歌|这首|歌|吧|呀|呗)?$',
    re.I)
_MUSIC_SONG_PHRASE_RE = re.compile(   # 搜一首周杰伦的歌 / 找首安静点的歌
    r'^(?:搜索|搜一下|搜|找一下|找)\s*(?:一|1)?\s*首\s*(?P<q>.+?)\s*(?:的)?\s*'
    r'(?:歌|歌曲|音乐)\s*(?:吧|呀|呗)?$', re.I)
_MUSIC_PLAY_RE = re.compile(          # 用网易云播放晴天 / 网易云里放一下稻香
    r'^(?:用|在|拿|打开)?\s*(?:网易云音乐|网易云|云音乐)\s*(?:里|上|里面|客户端)?\s*'
    r'(?:播放|放一下|放|听一下|听|来一首|来首|点播)\s*(?P<q>.*?)\s*(?:吧|呀|呗)?$',
    re.I)


def _web_command(text: str, raw: str):
    """网站 / 站内搜索 / 音乐收藏。不归这里管就返回 None。"""
    # 1) 音乐收藏优先：「收藏晴天」里的「收藏」不是搜索动词。
    for pattern in (_MUSIC_LIKE_PUT_RE, _MUSIC_LIKE_ADD_RE, _MUSIC_LIKE_VERB_RE):
        hit = pattern.match(text)
        if not hit:
            continue
        song = _MUSIC_LIKE_TARGET_NOISE.sub('', (hit.groupdict().get('song') or '')).strip()
        song = _SENTENCE_TAIL_RE.sub('', song).strip()
        if song in ('一首歌', '一首音乐', '首歌', '歌', '音乐', '歌曲', '这首',
                    '这', '那', '这个', '那个'):
            song = ''
        if pattern is _MUSIC_LIKE_VERB_RE and (len(song) < 2 or song in ('吗', '呢', '么')):
            return None  # 「喜欢吗」这种是闲聊，不是点歌
        if not song:
            return Command(action='music_like', target='', raw=raw,
                           note='要收藏哪一首？说「把晴天加入我喜欢」这样带歌名的。')
        if len(song) > 60 or _COMPOUND_RE.search(song):
            return None
        return Command(action='music_like', target=song, raw=raw)

    # 1.5)「搜一首周杰伦的歌」：带「首…歌」的是找歌，不是找网页
    song_phrase = _MUSIC_SONG_PHRASE_RE.fullmatch(text)
    if song_phrase:
        query = _SENTENCE_TAIL_RE.sub('', song_phrase.group('q')).strip()
        if query and len(query) <= 60:
            return Command(action='music_search', target=query, site='网易云音乐',
                           raw=raw)

    # 1.6)「用网易云播放晴天」：在客户端里放，不是开网页
    play_hit = _MUSIC_PLAY_RE.fullmatch(text)
    if play_hit:
        song = _SENTENCE_TAIL_RE.sub('', play_hit.group('q')).strip()
        if song in ('歌', '一首歌', '首歌', '音乐', '歌曲', '点歌', '首',
                    '这首歌', '这首', '那个', '这个'):
            song = ''
        if len(song) > 60 or _COMPOUND_RE.search(song):
            return None
        return Command(action='music_play', target=song, raw=raw,
                       note='' if song else '想听哪一首？说「用网易云播放晴天」。')

    # 2) 打开网站（没名字 / 名字带「网站」）
    if _OPEN_SITE_ANY_RE.fullmatch(text):
        return Command(action='open_site', target='', raw=raw,
                       note='已打开浏览器；想搜什么直接说「在 B 站搜猫」。')
    named = _OPEN_SITE_NAMED_RE.fullmatch(text)
    if named:
        name = _SENTENCE_TAIL_RE.sub('', named.group('name')).strip()
        # 「打开一个小网站」这种只有一个字的，当成没给名字，直接开浏览器。
        if len(name) <= 1:
            return Command(action='open_site', target='', raw=raw)
        if len(name) <= 60:
            return Command(action='open_site', target=name, raw=raw)

    # 3) 站内搜索（站点名必须是已知的，避免把「帮我搜索晴天」认成站名）
    probe = _SEARCH_PREFIX_RE.sub('', text).strip()
    hit = _SITE_SEARCH_RE.fullmatch(probe)
    if hit:
        site = hit.group('site')
        query = _SENTENCE_TAIL_RE.sub('', hit.group('q')).strip()
        site_hit = web_lib.resolve(site)
        if not query or len(query) > 200 or site_hit is None:
            return None
        if re.fullmatch(r'(?:索|寻|查|找|一?下)', query):
            return None  # 「在知乎搜索」只剩动词碎片，不是搜索请求
        if site_hit.get('music') and site_hit['name'] == '网易云音乐':
            return Command(action='music_search', target=query, site=site_hit['name'],
                           raw=raw)
        return Command(action='site_search', target=query, site=site_hit['name'],
                       raw=raw)
    return None


# 歌名里常带的赘词：「这首」「歌曲」等
_MUSIC_LIKE_TARGET_NOISE = re.compile(r'^(?:把|将|帮我|给我|请|这首|这首歌|歌曲|歌|音乐)+')


def _clean(text: str) -> str:
    """去掉开头的称呼与语气前缀，保留主体。"""
    value = str(text or '').strip()
    value = re.sub(r'^(?:咕嘎|小企鹅|企鹅|喂|嗯|那个|麻烦|请|请你|帮我|给我|快|来)+', '', value)
    value = re.sub(r'^[，,、\s]+', '', value)
    return value.strip()


def _resolve_app(name: str) -> str:
    """口语名 -> 宿主认识的软件名；不认识就原样返回，交给宿主查已安装软件。"""
    key = name.strip().strip('“”"\' ').casefold()
    return APP_ALIASES.get(key, name.strip().strip('“”"\' '))


def _clean_game_name(text: str) -> str:
    """剥掉书名号、结尾语气词和「我的」这类前缀。"""
    value = _TAIL_NOISE_RE.sub('', str(text or '')).strip().strip('“”"\'《》「」 ')
    value = re.sub(r'^(?:我的|我那个|那个|这个)', '', value).strip()
    return value


def _looks_like_steam_game(name: str) -> bool:
    """名字像不像 Steam 上的游戏。

    认识的游戏名就算本机没装，也要说「Steam 库里没有」而不是「未找到该软件」，
    这样用户知道是没安装，而不是咕嘎不认识。
    """
    lowered = str(name or '').casefold()
    if not lowered:
        return False
    return any(word.casefold() in lowered for word in steam_lib.GAME_NAME_HINTS)


def _steam_lookup(name: str):
    """查本机 Steam 库。任何异常都吞掉：插件绝不能因为读不到 Steam 而崩。"""
    try:
        return steam_lib.match(name)
    except Exception:  # noqa: BLE001
        return None


def is_known_desktop(name: str) -> bool:
    """是不是插件认识的桌面软件（微信、记事本、b站…）。

    是的话交回原生 ``open_app`` 通道执行——那边会把启动放进后台线程、
    复用已有窗口；不是的话插件一律放行，不抢原生和智能体的路由。
    """
    return str(name or '').casefold() in _DESKTOP_ONLY


def _steam_game_command(hit: dict, query: str, raw: str) -> Command:
    """匹配结果 -> Command；多候选时让用户说全名，不替用户猜。"""
    candidates = hit.get('candidates') or []
    if candidates:
        names = ' / '.join(item['name'] for item in candidates)
        return Command(action='open_steam_game', target=query, appid=0, raw=raw,
                       note='「%s」能对上多个游戏：%s。请说完整名字。' % (query, names))
    appid = int(hit.get('appid') or 0)
    title = str(hit.get('name') or query)
    return Command(action='open_steam_game', target=title, appid=appid, raw=raw,
                   note='已通过 Steam 启动《%s》。' % title)


def _file_command(text: str, raw: str):
    """「打开 D:\\报告.pdf」「把桌面上的合同打开」-> open_file；不是文件返回 None。"""
    target = ''
    for pattern in (_OPEN_INVERTED_RE, _SEE_FILE_RE, _OPEN_PREFIX_RE, _OPEN_SUFFIX_RE):
        hit = pattern.match(text)
        if hit:
            target = hit.group(1)
            break
    if not target:
        return None
    target = re.sub(r'^(?:文件|文件夹|目录)\s*[：:]?\s*', '', target.strip()).strip()
    if not target:
        return Command(action='help', raw=raw,
                       note='要打开哪个文件？可以说完整路径，例如「打开 D:\\报告.pdf」，'
                            '或者说「打开桌面上的合同.pdf」。')
    if _FILE_COMPOUND_RE.search(target):
        return None
    if files_lib.looks_like_path(target):
        return Command(action='open_file', target=target, raw=raw,
                       note='打开本机文件需要「本机文件权限」；没授权会直接拒绝并说明。')
    return None


def _find_game(text: str) -> tuple[str, str]:
    """在句子里找游戏名。返回 (标识, 原词)；没提到返回 ('', '')。"""
    lowered = text.casefold()
    for word in sorted(GAME_ALIASES, key=len, reverse=True):
        if word.casefold() in lowered:
            return GAME_ALIASES[word], word
    for word in OTHER_GAME_WORDS:
        if word.casefold() in lowered:
            return 'other', word
    return '', ''


def _budget(text: str) -> tuple[int, int]:
    """从「打 30 步」「玩 5 分钟」里读预算，并夹到项目允许的范围。"""
    actions, seconds = DEFAULT_MAX_ACTIONS, DEFAULT_SECONDS
    hit = re.search(r'(\d{1,4})\s*(?:步|个操作|次)', text)
    if hit:
        actions = max(1, min(MAX_ACTIONS_CEILING, int(hit.group(1))))
    hit = re.search(r'(\d{1,4})\s*分钟', text)
    if hit:
        seconds = max(10, min(SECONDS_CEILING, int(hit.group(1)) * 60))
    hit = re.search(r'(\d{1,4})\s*秒', text)
    if hit:
        seconds = max(10, min(SECONDS_CEILING, int(hit.group(1))))
    return actions, seconds


def parse(text: str) -> Command:
    """把一句中文解析成 Command。**只解析，不执行任何动作。**"""
    raw = str(text or '')
    value = _clean(raw)
    if not value:
        return Command(action='unknown', raw=raw)
    # 语气词只影响「把 X 打开吧」这类句子的结尾，单独留一份给打开类规则用。
    tail_free = _SENTENCE_TAIL_RE.sub('', value).strip()
    # 「帮我玩炉石」里的「帮我」会被 _clean 剥掉，所以触发词再对原文兜一次。
    auto_hit = bool(_AUTO_RE.search(value) or _AUTO_RE.search(raw))

    # 1) 停止优先级最高：任何时候都要能立刻叫停。
    if _STOP_RE.fullmatch(value) or _STOP_RE.fullmatch(raw.strip()) or _STOP_CONTEXT_RE.search(value):
        return Command(action='bg_stop', raw=raw,
                       note='已请求停止；正在返回的模型结果不会触发新输入。')

    # 2) 状态查询
    if _STATUS_RE.search(value):
        return Command(action='bg_status', raw=raw)

    # 3) 观战 / 单步
    if _OBSERVE_RE.search(value):
        return Command(action='bg_observe', raw=raw,
                       note='只读观察，不发送任何游戏输入。')
    if _STEP_RE.search(value):
        actions, seconds = _budget(value)
        return Command(action='bg_step', raw=raw, max_actions=1, seconds=seconds,
                       note='只执行一步，完成后主动报告日志回执。')

    # 4) 自动玩游戏：必须出现明确的「自己玩 / 帮我打 / 自动」才允许。
    if auto_hit:
        actions, seconds = _budget(value)
        game, word = _find_game(value)
        if game == 'other':
            return Command(action='unsupported_game', target=word, game='other', raw=raw,
                           note='目前只有炉石酒馆战棋能自动操作；其他游戏我只能帮你打开。'
                                '可以说「打开%s」。' % word)
        if not game:
            game = 'battleground'
            hint = '没有指明游戏，默认按炉石酒馆战棋处理。'
        else:
            hint = ''
        if game not in AUTO_PLAYABLE:
            return Command(action='unsupported_game', target=word, game=game, raw=raw,
                           note='这个游戏暂时没有自动操作后端。')
        return Command(action='auto_play', game=game, max_actions=actions,
                       seconds=seconds, raw=raw,
                       note=(hint + '已启动限时后台接管：最多 %d 步 / %d 秒，'
                             '随时说「停一下」可中止。启动不等于操作已成功，'
                             '完成后会以日志回执为准。' % (actions, seconds)).strip())

    # 4.5) 本机文件：盘符 / 扩展名 / 「桌面上的X」，走文件通道（需要文件权限）
    if _FILE_ON_RE.match(tail_free):
        return Command(action='file_access_on', raw=raw,
                       note='已开启本机文件权限；现在可以说「打开 D:\\报告.pdf」。')
    if _FILE_OFF_RE.match(tail_free):
        return Command(action='file_access_off', raw=raw,
                       note='已关闭本机文件权限，之后不会读取或打开本机文件。')
    file_cmd = _file_command(tail_free, raw)
    if file_cmd is not None:
        return file_cmd

    # 5) Steam：打开客户端 / 查游戏库 / 按名字开游戏
    if _STEAM_CLIENT_RE.fullmatch(tail_free):
        return Command(action='open_steam', raw=raw,
                       note='Steam 没运行的话会自己启动，首次可能要先等它更新。')
    if _STEAM_LIST_RE.search(value):
        return Command(action='list_steam_games', raw=raw)

    # 明确说到「steam 上的 X」「打开游戏 X」：没装也要说清楚是「库里没有」，
    # 而不是让用户看到「未找到该软件」这种莫名其妙的回复。
    explicit = ''
    for pattern in (_STEAM_GAME_PREFIX_RE, _STEAM_GAME_IN_RE, _OPEN_GAME_RE):
        matched = pattern.match(tail_free)
        if matched:
            explicit = _clean_game_name(matched.group(1))
            break
    if explicit and not _find_game(explicit)[0]:
        hit = _steam_lookup(explicit)
        if hit:
            return _steam_game_command(hit, explicit, raw)
        return Command(action='open_steam_game', target=explicit, appid=0, raw=raw,
                       note='Steam 库里没找到《%s》，执行时会列出已安装的游戏。' % explicit)

    # 「玩一下 X」只有在 X 确实在 Steam 库里才接，避免和打开软件的路由打架；
    # 炉石这类已有归属的游戏按「打开」处理，不自动代打。
    played = _PLAY_GAME_RE.match(tail_free)
    if played:
        candidate = _clean_game_name(played.group(1))
        if candidate:
            game, _word = _find_game(candidate)
            if game:
                return Command(action='open_app', target=_resolve_app(candidate), raw=raw)
            hit = _steam_lookup(candidate)
            if hit:
                return _steam_game_command(hit, candidate, raw)

    # 5.5) 网站 / 站内搜索 / 网易云收藏
    # 搜索本质是「拼一条 URL 再开浏览器」，本地就能做完；交给智能体反而会因为
    # 模型没配而报「域名解析失败」。放在打开软件之前，避免「在B站搜猫」被当成软件名。
    web_cmd = _web_command(tail_free, raw)
    if web_cmd is not None:
        return web_cmd

    # 6) 列出可打开的软件
    if _LIST_RE.search(value):
        return Command(action='list_apps', raw=raw)

    # 7) 帮助
    if _HELP_RE.search(value):
        return Command(action='help', raw=raw)

    # 8) 打开应用（三种语序）
    target = ''
    for pattern in (_OPEN_INVERTED_RE, _OPEN_PREFIX_RE, _OPEN_SUFFIX_RE):
        hit = pattern.match(tail_free)
        if hit:
            target = hit.group(1)
            break
    if target:
        target = _clean_game_name(target)
        if target in ('游戏', '一个游戏', '个游戏'):
            return Command(action='help', raw=raw,
                           note='要打开哪个游戏？可以说「打开游戏 缺氧」或「打开 steam 上的巫师3」。')
        if target and len(target) <= 40 and not _COMPOUND_RE.search(target):
            resolved = _resolve_app(target)
            folded = resolved.casefold()
            # 已知桌面软件（微信、记事本…）和炉石这类战网游戏保持原行为，
            # 不拿去做 Steam 匹配，免得把「打开微信」拐去查 Steam 库。
            if folded not in _DESKTOP_ONLY and folded not in _NON_STEAM_GAME_WORDS \
                    and target.casefold() not in _NON_STEAM_GAME_WORDS:
                hit = _steam_lookup(resolved) or _steam_lookup(target)
                if hit:
                    return _steam_game_command(hit, target, raw)
                if _looks_like_steam_game(target):
                    return Command(action='open_steam_game', target=target, appid=0, raw=raw,
                                   note='Steam 库里没找到《%s》，执行时会列出已安装的游戏。' % target)
            return Command(action='open_app', target=resolved, raw=raw)

    return Command(action='unknown', raw=raw)


# ---------------------------------------------------------------------------
# 动作计划与执行
# ---------------------------------------------------------------------------

HELP_TEXT = (
    '咕嘎指令台可以听懂这些话：\n'
    '· 打开微信 / 把记事本打开 / VS Code 开一下 —— 打开软件\n'
    '· 打开 steam —— 打开或切到 Steam 主窗口\n'
    '· 打开 steam 上的巫师3 / 玩一下大表哥2 / 打开游戏 缺氧 —— 通过 Steam 启动游戏\n'
    '· 我 steam 里有什么游戏 —— 列出库里已安装的游戏\n'
    '· 自己打一局战棋 / 帮我玩炉石 / 自动对战 30 步 —— 启动限时自动对战\n'
    '· 走一步 —— 只操作一步；观战 —— 只看不动\n'
    '· 看看打到哪了 —— 查进度；停一下 / 别玩了 —— 立刻停止\n'
    '· 有哪些应用 —— 列出能打开的软件\n'
    '· 打开 D:\\报告.pdf / 打开桌面上的合同 / 打开我的文档 —— 打开本机文件和文件夹'
    '（需要「本机文件权限」，启动时会问）\n'
    '· 允许读取文件 / 关闭文件权限 —— 随时开关本机文件权限\n'
    '· 在 B 站搜猫 / 用必应查天气 / 淘宝上搜机械键盘 —— 直接打开那个平台的搜索结果\n'
    '· 打开一个网站 / 打开小破站官网 —— 打开网站；认不出的名字会先搜出来给你点\n'
    '· 网易云音乐搜晴天 / 搜一首周杰伦的歌 —— 在网易云客户端里搜，结果就在客户端窗口\n'
    '· 用网易云播放晴天 / 网易云里放一下稻香 —— 在客户端里搜并播放第一条\n'
    '· 把晴天加入我喜欢 / 收藏周杰伦的晴天 —— 在客户端里点搜索结果那行的 ♥\n'
    '注意：游戏名从本机 Steam 库读取，没安装的游戏会如实告诉你「库里没有」。'
    '目前只有炉石传说「酒馆战棋」的招募阶段支持受控自动操作，'
    'Steam 游戏只负责启动，不代打。'
    '搜索只是拼地址、开浏览器，不走模型，断网就直说；'
    '收藏要在浏览器里点一下 ♥，需要开启「完整访问」，没登录会失败并如实告诉你。'
    '在客户端里操作需要「完整访问」，且客户端要在前台（咕嘎会自己切过去）；'
    '点完 ♥ 一定会再看一眼是不是真的变红，没变红就说没确认到。'
)


def plan_for(cmd: Command) -> list:
    """Command -> [(工具名, 参数, 这一步的中文说明)]。

    工具名必须是 ``core/self_agent.py`` 的 TOOLS 里已有的名字，
    这样无论从聊天、语音还是模型工具循环进来，都能落到同一份实现。
    """
    if cmd.action == 'open_app':
        return [('desktop_command', dict(action='open', target=cmd.target),
                 '打开 ' + cmd.target)]
    if cmd.action == 'open_local_app':
        # 原生白名单里没有、但本机确实装着的软件：插件自己按快捷方式/exe 启动。
        return [('local.open_app', dict(target=cmd.target), '打开 ' + cmd.target)]
    if cmd.action == 'open_file':
        # 文件读取属于「本机文件权限」范围，执行前会再验一次授权。
        return [('local.open_file', dict(target=cmd.target), '打开 ' + cmd.target)]
    if cmd.action == 'file_access_on':
        return [('local.file_access', dict(granted=True), '开启本机文件权限')]
    if cmd.action == 'file_access_off':
        return [('local.file_access', dict(granted=False), '关闭本机文件权限')]
    if cmd.action == 'site_search':
        return [('local.site_search', dict(site=cmd.site, query=cmd.target),
                 '在%s搜索「%s」' % (cmd.site, cmd.target))]
    if cmd.action == 'open_site':
        return [('local.open_site', dict(target=cmd.target),
                 '打开网站' + ('：' + cmd.target if cmd.target else '（让你现选）'))]
    if cmd.action == 'music_search':
        return [('local.music_search', dict(query=cmd.target),
                 '在网易云音乐搜索「%s」' % cmd.target)]
    if cmd.action == 'music_like':
        return [('local.music_like', dict(query=cmd.target),
                 '把《%s》加进我喜欢的音乐' % cmd.target if cmd.target
                 else '收藏一首歌')]
    if cmd.action == 'music_play':
        return [('local.music_play', dict(query=cmd.target),
                 '在网易云音乐播放「%s」' % cmd.target if cmd.target
                 else '在网易云音乐播放')]
    if cmd.action == 'open_steam':
        return [('steam.open_client', {}, '打开 Steam 客户端')]
    if cmd.action == 'open_steam_game':
        return [('steam.rungame', dict(appid=cmd.appid, name=cmd.target),
                 '通过 Steam 启动 ' + cmd.target)]
    if cmd.action == 'list_steam_games':
        return [('steam.list_games', {}, '列出 Steam 游戏库')]
    if cmd.action == 'auto_play':
        return [('battleground',
                 dict(action='auto', max_actions=cmd.max_actions, seconds=cmd.seconds),
                 '启动限时自动对战')]
    if cmd.action == 'bg_step':
        return [('battleground', dict(action='step', max_actions=1, seconds=cmd.seconds),
                 '执行一步')]
    if cmd.action == 'bg_observe':
        return [('battleground', dict(action='observe', max_actions=12, seconds=240),
                 '只读观战')]
    if cmd.action == 'bg_status':
        return [('battleground', dict(action='status'), '查询后台状态')]
    if cmd.action == 'bg_stop':
        return [('battleground', dict(action='stop'), '停止后台任务')]
    if cmd.action == 'list_apps':
        return [('desktop_command', dict(action='list'), '列出可打开的软件')]
    return []


def _message_for(cmd: Command, receipts: list) -> str:
    """给用户听的一句话回执。宁可说清楚边界，也不报喜不报忧。"""
    if cmd.action == 'help':
        return HELP_TEXT
    if cmd.action == 'unsupported_game':
        return cmd.note
    if cmd.action == 'unknown':
        return '没听懂这条指令。可以说「怎么用」看支持的命令。'
    if not receipts:
        return '没有需要执行的动作。'
    last = receipts[-1]
    if cmd.action == 'music_like':
        # 收藏/播放的失败原因本身就是要说的话（没登录 / 没找到♥ / 界面变了），
        # 不要再套一层「这一步没成功」。
        payload = last.get('result') or {}
        if last.get('ok'):
            return str(payload.get('message') or '已加进我喜欢的音乐。')
        return str(payload.get('error') or '没收藏成功。')
    if cmd.action == 'music_play':
        payload = last.get('result') or {}
        if last.get('ok'):
            return str(payload.get('message') or '已经在网易云里放上了。')
        return str(payload.get('error') or '没能播放。')
    if not last.get('ok'):
        return '这一步没成功：' + str(last.get('error') or '宿主没有返回成功回执')[:160]
    payload = last.get('result') or {}
    if cmd.action in ('site_search', 'open_site', 'music_search'):
        return str(payload.get('message') or cmd.note or '已在浏览器打开。')
    if cmd.action in ('open_steam', 'open_steam_game'):
        return str(payload.get('message') or cmd.note or '已发出启动请求。')
    if cmd.action == 'list_steam_games':
        total = payload.get('total')
        return '你的 Steam 库里已安装 %s 个游戏，已列在面板里。' % (
            total if total is not None else '若干')
    if cmd.action == 'list_apps':
        result = last.get('result') or {}
        total = result.get('total') if isinstance(result, dict) else None
        return '查到了可打开的软件' + ('共 %s 个' % total if total is not None else '') + '，已列在面板里。'
    if cmd.action == 'bg_status':
        return cmd.note or '已取回战棋后台状态，详情看面板。'
    return cmd.note or ('已完成：' + last.get('label', '动作'))


#: 这些工具由插件自己完成，不走宿主 runtime：
#: Steam 的启动协议不在 desktop_command 的能力范围里，也没必要惊动宿主的软件扫描。
LOCAL_TOOLS = frozenset(('steam.open_client', 'steam.list_games', 'steam.rungame',
                         'local.open_app', 'local.open_file', 'local.file_access',
                         'local.site_search', 'local.open_site',
                         'local.music_search', 'local.music_like', 'local.music_play'))


def run(cmd: Command, call: Optional[Callable[[str, dict], dict]] = None) -> dict:
    """执行一条命令。``call(tool, args)`` 由宿主注入；不注入就用默认查找。

    返回的 dict 一定带 ``ok``、``message``、``receipts``，方便宿主直接播报。
    """
    invoke = call or default_call
    plan = plan_for(cmd)
    receipts = []
    ok = True
    for tool, args, label in plan:
        try:
            if tool in LOCAL_TOOLS:
                result = _normalize(tool, _local_execute(tool, dict(args)))
            else:
                result = _normalize(tool, invoke(tool, dict(args)))
        except Exception as exc:  # noqa: BLE001 - 插件不能把宿主聊天线程带崩
            result = dict(ok=False, error='%s: %s' % (type(exc).__name__, str(exc)[:200]))
        receipts.append(dict(tool=tool, label=label, ok=bool(result.get('ok')),
                             error=str(result.get('error') or ''), result=result))
        if not result.get('ok'):
            ok = False
            break
    return dict(ok=ok, message=_message_for(cmd, receipts),
                action=cmd.action, receipts=receipts, command=cmd.as_dict())


#: 客户端这条路走不通（没装 / 界面认不出）时，退回浏览器网页版
_CLIENT_FALLBACK_CODES = ('no_client', 'ui_not_found')


def _exact_query(query: str):
    """把「晴天」补成「晴天 周杰伦」：客户端按相关性排，第一条才更可能是那一版。

    返回 ``(搜索词, 歌名)``；查不到就原样返回，不编歌手。
    """
    text = str(query or '').strip()
    try:
        found = music_lib.pick(text)
    except Exception:  # noqa: BLE001 - 断网就让客户端自己搜
        return text, ''
    song = found.get('song') if found.get('ok') else None
    if not song:
        return text, ''
    artists = song.get('artists') or ''
    return (song.get('name') or text) + ((' ' + artists) if artists else ''), song.get('name') or ''


def _music_search(query: str) -> dict:
    """优先在网易云客户端里搜；客户端用不了才退回网页版。"""
    text = str(query or '').strip()
    result = cloudmusic_lib.search(text)
    if result.get('ok') or result.get('code') not in _CLIENT_FALLBACK_CODES:
        return result
    web = music_lib.open_search(text)
    if web.get('ok'):
        web['message'] = (web.get('message') or '') + '（客户端这次没用上，开的是网页版）'
        web['client_error'] = result.get('error')
    return web


def _music_play(query: str) -> dict:
    text, _song = _exact_query(query)
    if not text:
        return dict(ok=False, error='想听哪一首？说「用网易云播放晴天」。')
    return cloudmusic_lib.play(text)


def _music_like(query: str) -> dict:
    """优先在客户端里点 ♥；只有「没装 / 认不出界面」才退回网页版（避免点两次）。"""
    text, song = _exact_query(query)
    result = cloudmusic_lib.like(text, song=song)
    if result.get('ok') or result.get('code') not in _CLIENT_FALLBACK_CODES:
        return result
    web = music_lib.like_song(str(query or '').strip())
    if web.get('ok'):
        web['message'] = (web.get('message') or '') + '（客户端里没认出来，这次在网页版点的 ♥）'
    elif web.get('error'):
        web['error'] = (str(web.get('error')) + '\n客户端那边：'
                        + str(result.get('error') or ''))
    return web


def _local_execute(tool: str, args: dict) -> dict:
    """插件内部执行的动作。全部返回 {ok, message/error}，不抛异常。"""
    if tool == 'local.open_app':
        return app_lib.launch(str(args.get('target') or ''))
    if tool == 'local.open_file':
        return files_lib.open_target(str(args.get('target') or ''))
    if tool == 'local.file_access':
        granted = bool(args.get('granted'))
        data = consent.set_granted(granted, source='command')
        if granted:
            return dict(ok=True, granted=True, data=data,
                        message='已开启本机文件权限。现在可以说「打开 D:\\报告.pdf」。')
        return dict(ok=True, granted=False, data=data,
                    message='已关闭本机文件权限，之后不会读取或打开本机文件。')
    if tool == 'local.site_search':
        return web_lib.open_search(str(args.get('site') or ''),
                                   str(args.get('query') or ''))
    if tool == 'local.open_site':
        return web_lib.open_site(str(args.get('target') or ''))
    if tool == 'local.music_search':
        return _music_search(str(args.get('query') or ''))
    if tool == 'local.music_like':
        return _music_like(str(args.get('query') or ''))
    if tool == 'local.music_play':
        return _music_play(str(args.get('query') or ''))
    if tool == 'steam.open_client':
        return steam_lib.open_client()
    if tool == 'steam.list_games':
        games = steam_lib.installed_games()
        items = [dict(appid=game['appid'], name=game['name'], ready=bool(game['ready']))
                 for game in games]
        return dict(ok=True, total=len(items), games=items,
                    message='Steam 库里已安装 %d 个游戏。' % len(items))
    if tool == 'steam.rungame':
        appid = int(args.get('appid') or 0)
        name = str(args.get('name') or '')
        if appid > 0:
            return steam_lib.launch_game(appid, name)
        # 解析阶段没定下 AppID（库里没装 / 多个候选）：执行时再查一次，
        # 缓存可能已经刷新，也方便把「没找到」说清楚。
        found = _steam_lookup(name)
        if not found:
            games = steam_lib.installed_games()
            sample = '、'.join(game['name'] for game in games[:8])
            hint = ('已经装好的有：' + sample + '。') if sample else '没有读到任何已安装的游戏。'
            return dict(ok=False, error='Steam 库里没找到「%s」。%s' % (name, hint))
        if found.get('candidates'):
            names = ' / '.join(item['name'] for item in found['candidates'])
            return dict(ok=False, error='「%s」能对上多个游戏：%s。请说完整名字。' % (name, names))
        return steam_lib.launch_game(found['appid'], found['name'])
    return dict(ok=False, error='未知的本地工具：' + tool)


def _normalize(tool: str, result) -> dict:
    """宿主某些工具（battleground）不返回 ok 字段，这里补齐，别误判成失败。"""
    if not isinstance(result, dict):
        return dict(ok=bool(result))
    value = dict(result)
    if 'ok' not in value:
        value['ok'] = not bool(value.get('error'))
    return value


# ---------------------------------------------------------------------------
# 宿主绑定：默认执行器
# ---------------------------------------------------------------------------

_RUNTIME = None


def bind(runtime) -> None:
    """宿主显式绑定 AppRuntime（可选）。绑定后优先用它，查找更稳。"""
    global _RUNTIME
    _RUNTIME = runtime


def bound_runtime():
    """返回当前绑定的 runtime，没绑定返回 None。"""
    return _RUNTIME


def find_runtime():
    """在已运行的 Qt 界面里找聊天面板持有的 AppRuntime。

    ``ui/agent_chat.py`` 里 ``self.runtime = AppRuntime(pet, self, self.store)``，
    面板是顶层窗口，所以遍历 topLevelWidgets 就能拿到，不需要改第二个核心文件。
    """
    if _RUNTIME is not None:
        return _RUNTIME
    try:
        from PyQt5.QtWidgets import QApplication  # 延迟导入：CLI/测试不需要 Qt
        app = QApplication.instance()
        if app is None:
            return None
        for widget in app.topLevelWidgets():
            candidate = getattr(widget, 'runtime', None)
            if candidate is not None and callable(getattr(candidate, 'execute', None)):
                return candidate
    except Exception:  # noqa: BLE001
        pass
    return None


def default_call(tool: str, args: dict) -> dict:
    """默认执行器：把工具调用转给宿主的 AppRuntime.execute(tool, args)。"""
    runtime = find_runtime()
    if runtime is None:
        return dict(ok=False,
                    error='没有连上咕嘎主界面。请先启动咕嘎（python launcher.py），'
                          '或单独运行本插件的演示模式查看解析结果。')
    return runtime.execute(tool, args)


# ---------------------------------------------------------------------------
# 分发物：插件市场卡片
# ---------------------------------------------------------------------------

def market_card() -> dict:
    """生成 ``plugins/community/<id>.json``（kind=workflow 的提示词卡片）。"""
    return {
        'schema': 1,
        'id': PLUGIN_ID,
        'name': '咕嘎指令台 · 打开应用 / 网站搜索 / 网易云收藏 / Steam 游戏 / 自己玩游戏',
        'description': '一句中文让咕嘎打开软件、打开网站、在 B 站/知乎/网易云直接搜、'
                       '把歌加进我喜欢的音乐、启动 Steam 游戏、开酒馆战棋限时自动对战。',
        'version': VERSION,
        'kind': 'workflow',
        'body': (
            '本机会话启用「咕嘎指令台」命令表。用户说「打开 X / 把 X 打开」时，'
            '用 desktop_command(action=open, target=中文软件名) 拉起或复用软件窗口；'
            '说「有哪些应用」时用 desktop_command(action=list)。'
            '本机文件：说「打开 D:\\报告.pdf」「打开桌面上的合同.docx」「打开我的文档」时，'
            '先确认本机文件权限（plugins/guga_commander/consent.py）已开启，'
            '再用 os.startfile 打开；未授权时必须明确拒绝并说明如何开启，绝不静默读取。'
            '用户说「允许读取文件 / 关闭文件权限」时同步开关该权限。'
            '网站与搜索（全部本地拼 URL，不经过模型）：说「在 B 站搜猫」「用必应查天气」'
            '「淘宝上搜机械键盘」时，用 plugins/guga_commander/web_lib.py 的站点表拼出'
            '该平台的搜索结果页并打开浏览器；站点表覆盖 B 站、知乎、微博、淘宝、京东、'
            '豆瓣、抖音、小红书、YouTube、GitHub、CSDN 等，认不出站点就如实说，'
            '不要编造网址。说「打开一个网站」「打开小破站官网」时打开对应首页；'
            '名字认不出来就先在必应搜出来给用户点，不得假装认识。'
            '网易云音乐（优先操作本机客户端，不是网页）：说「网易云音乐搜晴天」'
            '「搜一首周杰伦的歌」时，用 plugins/guga_commander/cloudmusic_lib.py 把客户端'
            '切到前台、在它的搜索框里搜；说「用网易云播放晴天」「网易云里放一下稻香」时'
            '搜索并播放第一条；说「把晴天加入我喜欢」「收藏周杰伦的晴天」时，搜索后点'
            '搜索结果第一行那颗 ♥，必须观察到它变成实心红才算成功，没变红就如实说'
            '「没确认到」，把窗口留在结果页让用户自己点，绝不谎报已收藏。'
            '客户端没装或界面认不出来时，才退回网页版 music_lib.like_song()，并说明这次'
            '是在网页版点的。这条路径靠真实键鼠（客户端是 CEF，UIA 读不到内部），需要'
            '「完整访问」已开启，未开启时直接说明如何开启。'
            'Steam 相关：说「打开 steam」用 steam://open/main 唤起客户端；'
            '说「打开 steam 上的 X」「打开游戏 X」「玩一下 X」时，先按名字匹配本机 '
            'Steam 库（plugins/guga_commander/steam_lib.py），命中后用 '
            'steam://rungameid/<AppID> 启动，AppID 必须来自本机 appmanifest，不得猜；'
            '说「我 steam 里有什么游戏」时列出已安装游戏。'
            '库里没有的游戏要如实说「Steam 库里没找到」并给出已安装示例，'
            '有多个同分候选时让用户说全名，不能替用户选；Steam 游戏只负责启动，绝不代打。'
            '用户**明确**说「自己玩 / 帮我打 / 自动对战 / 托管」时，'
            '才允许用 battleground(action=auto, max_actions=120, seconds=1200) 启动限时后台接管；'
            '「走一步」用 action=step，「观战」用 action=observe，'
            '「看看打到哪了」用 action=status，「停一下 / 别玩了」立刻用 action=stop。'
            '没有明确要求时绝不自动接管，netcut 必须由用户当场单独要求。'
            '预算上限 500 步 / 1800 秒，启动不等于操作成功，结论只以日志回执为准。'
            '目前只有炉石传说酒馆战棋的招募阶段具备受控自动操作，'
            '其他游戏只能帮忙打开，必须如实说明，不能声称可以完整代打。'
        ),
    }


# ---------------------------------------------------------------------------
# 命令行演示：不启动桌宠也能看解析结果
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import sys
    words = list(argv if argv is not None else sys.argv[1:])
    if not words:
        print(HELP_TEXT)
        print('\n用法：python commander.py "自己打一局战棋"')
        print('      python commander.py --demo     # 看全部示例的解析结果')
        return 0
    if words == ['--demo']:
        samples = (
            '打开微信', '把记事本打开吧', 'VS Code 开一下', '帮我启动炉石传说',
            '打开 D:\\报告.pdf', '打开桌面上的合同.docx', '打开我的文档',
            '允许读取文件', '关闭文件权限',
            '在B站搜索猫', '用必应查天气', '淘宝上搜机械键盘',
            '打开一个网站', '打开小破站官网',
            '网易云音乐搜晴天', '搜一首周杰伦的歌',
            '把晴天加入我喜欢', '帮我添加一首音乐进我的喜欢中',
            '打开 steam', '我 steam 里有什么游戏',
            '打开 steam 上的巫师3', '玩一下大表哥2', '打开游戏 缺氧',
            '自己打一局战棋', '帮我玩炉石 30 步', '自动对战 5 分钟',
            '自己玩原神', '走一步', '观战', '看看打到哪了', '停一下',
            '有哪些应用', '怎么用', '今天天气怎么样',
        )
        for sample in samples:
            cmd = parse(sample)
            extra = cmd.target or cmd.game or ''
            if cmd.appid:
                extra = '%s (AppID %s)' % (extra, cmd.appid)
            print('%-22s -> %-18s %s' % (sample, cmd.action, extra))
        return 0
    text = ' '.join(words)
    cmd = parse(text)
    print('原话：', text)
    print('解析：', cmd.as_dict())
    print('计划：', plan_for(cmd))
    print('说明：', cmd.note or HELP_TEXT.split('\n')[0])
    print('\n（演示模式只解析、不执行。要真正执行请先安装插件并启动咕嘎。）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
