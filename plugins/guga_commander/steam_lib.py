# -*- coding: utf-8 -*-
"""Steam 客户端与游戏库探测（纯标准库，不联网、不改注册表）。

为什么单独一个模块：

* Steam 游戏不是普通的「软件 exe」，唯一稳定的启动方式是系统协议
  ``steam://rungameid/<AppID>``，而 AppID 只能从本机 manifest 读出来，不能靠猜；
* 解析层要保持轻量，所以这里带 30 秒缓存，首次扫描通常几十毫秒。

只做三件事：**找到 Steam、列出已安装游戏、按名字匹配并启动**。
"""
from __future__ import annotations

import os
import re
import time

PROTOCOL_RUN = 'steam://rungameid/%d'
PROTOCOL_MAIN = 'steam://open/main'

#: 会出现在 steamapps 里、但不是游戏的运行时组件。
_NON_GAME_PATTERNS = (
    'steamworks common redistributables', 'steamvr', 'steam linux runtime',
    'proton', 'steam play', 'steam client', 'steam audio',
)

#: 中文说法 / 俗称 -> 商店名里的英文关键词。
#: 键会被 normalize（去标点、转小写）后做「包含」匹配，所以别放太短的词。
GAME_NAME_HINTS = {
    '黑神话': 'black myth', '悟空': 'black myth', '黑猴子': 'black myth',
    '艾尔登法环': 'elden ring', '法环': 'elden ring', '老头环': 'elden ring',
    '荒野大镖客': 'red dead', '大镖客': 'red dead', '大表哥': 'red dead', 'rdr': 'red dead',
    '巫师': 'witcher', '猎魔人': 'witcher',
    '赛博朋克': 'cyberpunk', '2077': 'cyberpunk',
    '博德之门': 'baldur', 'bg3': 'baldur',
    '星露谷物语': 'stardew', '星露谷': 'stardew',
    '泰拉瑞亚': 'terraria',
    '空洞骑士': 'hollow knight', '丝之歌': 'silksong',
    '反恐精英': 'counter-strike', '反恐': 'counter-strike',
    'csgo': 'counter-strike', 'cs2': 'counter-strike',
    '刀塔': 'dota',
    '绝地求生': 'pubg', '吃鸡': 'pubg',
    '给他爱': 'grand theft auto', 'gta': 'grand theft auto',
    '文明': 'civilization', '文明6': 'civilization',
    '饥荒': 'dont starve',
    '双人成行': 'it takes two', '双影奇境': 'split fiction',
    '胡闹厨房': 'overcooked', '分手厨房': 'overcooked',
    '只狼': 'sekiro',
    '辐射': 'fallout',
    '上古卷轴': 'elder scrolls', '老滚': 'elder scrolls', '天际': 'skyrim',
    '帝国时代': 'age of empires', '全面战争': 'total war',
    '欧洲卡车': 'euro truck', '欧卡': 'euro truck',
    '模拟人生': 'sims', 'apex': 'apex', '命运': 'destiny',
    '星际战甲': 'warframe', '彩虹六号': 'rainbow six', 'r6': 'rainbow six',
    '求生之路': 'left 4 dead', '传送门': 'portal', '半条命': 'half-life',
    '方舟': 'ark', '无人深空': 'no man', '死亡搁浅': 'death stranding',
    '底特律': 'detroit', '极限竞速': 'forza', '地平线': 'forza',
    '仁王': 'nioh', '怪物猎人': 'monster hunter', '最终幻想': 'final fantasy',
    '刺客信条': 'assassin', '孤岛惊魂': 'far cry', '看门狗': 'watch dogs',
    '地铁': 'metro', '生化危机': 'resident evil', '鬼泣': 'devil may cry',
    '尼尔': 'nier', '三国志': 'romance of the three kingdoms',
    '足球经理': 'football manager', '极品飞车': 'need for speed',
    '死亡空间': 'dead space', '行尸走肉': 'walking dead',
    '迈阿密热线': 'hotline miami', '围攻': 'besiege',
    '壁纸引擎': 'wallpaper engine', '动态壁纸': 'wallpaper engine',
    '缺氧': 'oxygen not included', '僵尸毁灭工程': 'project zomboid', '僵毁': 'project zomboid',
    '台球': 'pool', '兔子人': 'bunny man', '索尼克': 'sonic',
    '橡皮强盗': 'rubber bandits', '炸弹香蕉': 'bombanana',
    '深海迷航': 'subnautica', '死亡细胞': 'dead cells', '哈迪斯': 'hades',
    '杀戮尖塔': 'slay the spire', '环世界': 'rimworld', '异星工厂': 'factorio',
    '暗黑破坏神': 'diablo', '魔兽争霸': 'warcraft', '星际争霸': 'starcraft',
    '守望先锋': 'overwatch', '使命召唤': 'call of duty', '战地': 'battlefield',
    '泰坦陨落': 'titanfall', '无主之地': 'borderlands', '生化奇兵': 'bioshock',
    '光环': 'halo',
}

#: 阿拉伯数字 -> 罗马数字。商店名里 GTA V、文明 VI 这类写法很常见。
_ROMAN = {'2': 'ii', '3': 'iii', '4': 'iv', '5': 'v', '6': 'vi',
          '7': 'vii', '8': 'viii', '9': 'ix', '10': 'x'}

_CACHE = {'at': 0.0, 'steam_dir': '', 'games': []}
_CACHE_TTL = 30.0


# ---------------------------------------------------------------------------
# 定位 Steam
# ---------------------------------------------------------------------------

def _registry_paths():
    """从注册表读 SteamPath / InstallPath（只读，不写）。"""
    if os.name != 'nt':
        return []
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key in (r'Software\Valve\Steam', r'SOFTWARE\WOW6432Node\Valve\Steam'):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    for name in ('SteamPath', 'InstallPath'):
                        try:
                            value = winreg.QueryValueEx(handle, name)[0]
                        except OSError:
                            continue
                        if value:
                            found.append(str(value))
            except OSError:
                continue
    return found


def find_steam_dir() -> str:
    """返回 Steam 安装目录；找不到返回空串。"""
    candidates = list(_registry_paths())
    for env, leaf in (('PROGRAMFILES(X86)', 'Steam'), ('PROGRAMFILES', 'Steam'),
                      ('LOCALAPPDATA', 'Steam')):
        root = os.environ.get(env)
        if root:
            candidates.append(os.path.join(root, leaf))
    candidates += [r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam',
                   r'D:\Steam', r'D:\SteamLibrary']
    for path in candidates:
        try:
            fixed = os.path.normpath(path.replace('/', os.sep))
        except (TypeError, ValueError):
            continue
        if os.path.isfile(os.path.join(fixed, 'steam.exe')) or \
                os.path.isdir(os.path.join(fixed, 'steamapps')):
            return fixed
    return ''


def _vdf_unescape(value: str) -> str:
    """VDF 里的路径是 ``D:\\\\games`` 这种双反斜杠写法。"""
    return value.replace('\\\\', '\\')


def _library_paths(steam_dir: str):
    """根库 + libraryfolders.vdf 里登记的其它库。"""
    paths = [steam_dir]
    vdf = os.path.join(steam_dir, 'steamapps', 'libraryfolders.vdf')
    try:
        with open(vdf, encoding='utf-8', errors='replace') as handle:
            text = handle.read()
    except OSError:
        return paths
    for hit in re.finditer(r'"path"\s+"([^"]+)"', text):
        value = os.path.normpath(_vdf_unescape(hit.group(1)))
        if value and value not in paths:
            paths.append(value)
    return paths


def _acf_field(text: str, key: str) -> str:
    hit = re.search(r'"%s"\s+"([^"]*)"' % re.escape(key), text)
    return hit.group(1) if hit else ''


def _is_non_game(title: str) -> bool:
    lowered = title.casefold()
    return any(word in lowered for word in _NON_GAME_PATTERNS)


def _scan(steam_dir: str):
    """扫所有库的 appmanifest_*.acf。不递归，只读顶层。"""
    games, seen = [], set()
    for library in _library_paths(steam_dir):
        app_dir = os.path.join(library, 'steamapps')
        try:
            names = os.listdir(app_dir)
        except OSError:
            continue
        for name in names:
            if not (name.startswith('appmanifest_') and name.endswith('.acf')):
                continue
            try:
                with open(os.path.join(app_dir, name), encoding='utf-8', errors='replace') as handle:
                    text = handle.read()
            except OSError:
                continue
            appid, title = _acf_field(text, 'appid'), _acf_field(text, 'name')
            if not appid.isdigit() or not title or int(appid) in seen:
                continue
            seen.add(int(appid))
            flags = _acf_field(text, 'StateFlags')
            games.append(dict(
                appid=int(appid),
                name=title,
                library=library,
                installdir=_acf_field(text, 'installdir'),
                size=int(_acf_field(text, 'SizeOnDisk') or 0),
                ready=(int(flags) & 4) == 4 if flags.isdigit() else True,
            ))
    games = [game for game in games if not _is_non_game(game['name'])]
    games.sort(key=lambda game: (not game['ready'], game['name'].casefold()))
    return games


def installed_games(force: bool = False):
    """已安装游戏列表（带 TTL 缓存）。Steam 未安装时返回空列表。

    空结果也要缓存：否则没装 Steam 的机器每说一句「打开 X」都要读一遍注册表。
    """
    now = time.monotonic()
    if not force and _CACHE['at'] and now - _CACHE['at'] < _CACHE_TTL:
        return list(_CACHE['games'])
    steam_dir = find_steam_dir()
    games = _scan(steam_dir) if steam_dir else []
    _CACHE.update(at=now, steam_dir=steam_dir, games=games)
    return list(games)


def cache_info() -> dict:
    return dict(steam_dir=_CACHE['steam_dir'], count=len(_CACHE['games']),
                age=round(time.monotonic() - _CACHE['at'], 1) if _CACHE['at'] else None)


# ---------------------------------------------------------------------------
# 名称匹配
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """归一化：小写、去掉标点空格、保留中文与数字。

    ``The Witcher 3: Wild Hunt — Remastered`` -> ``thewitcher3wildhuntremastered``
    """
    value = str(text or '').casefold()
    value = value.replace('：', ':').replace('·', '').replace('—', '-').replace('–', '-')
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', value)


def _digits_ok(name_norm: str, digits: str) -> bool:
    """「巫师3」「大表哥2」「gta5」里的数字要能在游戏名里对上。"""
    if not digits:
        return True
    if all(digit in name_norm for digit in digits):
        return True
    for digit in digits:  # GTA V、文明 VI 这类罗马数字写法
        roman = _ROMAN.get(digit)
        if not (roman and roman in name_norm):
            return False
    return True


def _score(game: dict, query_norm: str, hint_keyword: str = '') -> int:
    name_norm = normalize(game['name'])
    if not name_norm:
        return 0
    if name_norm == query_norm:
        return 100
    if hint_keyword:
        keyword = normalize(hint_keyword)
        if keyword and keyword in name_norm:
            # 名字越短，说明越贴题（避免「Witcher 3」输给某个合集）
            return 90 - min(len(name_norm) // 40, 8)
    if len(query_norm) >= 2 and query_norm in name_norm:
        return 80 - min(len(name_norm) // 40, 8)
    if len(name_norm) >= 3 and name_norm in query_norm:
        return 70
    return 0


def match(query: str, games=None):
    """按名字找游戏。返回 None 或 ``dict(appid, name, ready, score, candidates)``。

    ``games`` 可注入（测试用），默认读本机 Steam 库。
    """
    query_norm = normalize(query)
    if not query_norm:
        return None
    pool = list(games) if games is not None else installed_games()
    if not pool:
        return None
    digits = ''.join(ch for ch in query_norm if ch.isdigit())

    # 最长优先：「巫师3」应压过「巫师」
    hint = ''
    for word in sorted(GAME_NAME_HINTS, key=len, reverse=True):
        if normalize(word) and normalize(word) in query_norm:
            hint = GAME_NAME_HINTS[word]
            break

    scored = []
    for game in pool:
        name_norm = normalize(game['name'])
        if not _digits_ok(name_norm, digits):
            continue
        score = _score(game, query_norm, hint)
        if score:
            scored.append((score, len(name_norm), game))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score = scored[0][0]
    winners = [item for item in scored if item[0] == best_score]
    best = dict(winners[0][2])
    best['score'] = best_score
    best['candidates'] = [dict(appid=item[2]['appid'], name=item[2]['name'])
                          for item in winners[:8]] if len(winners) > 1 else []
    return best


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def open_client() -> dict:
    """打开（或切到）Steam 主窗口。"""
    steam_dir = find_steam_dir()
    if not steam_dir:
        return dict(ok=False, error='这台电脑上没找到 Steam 客户端，请先安装 Steam。')
    try:
        os.startfile(PROTOCOL_MAIN)  # noqa: S606 - 走系统注册的 steam:// 协议
    except OSError as exc:
        return dict(ok=False, error='系统没能调起 Steam：%s' % exc, steam_dir=steam_dir)
    return dict(ok=True, steam_dir=steam_dir,
                message='已请求打开 Steam 主窗口；如果原本没运行，它会自己启动（首次可能较慢）。')


def launch_game(appid, name: str = '') -> dict:
    """通过 ``steam://rungameid/<AppID>`` 启动游戏。AppID 必须是正整数。"""
    try:
        value = int(appid)
    except (TypeError, ValueError):
        return dict(ok=False, error='AppID 无效，拒绝启动。')
    if value <= 0:
        return dict(ok=False, error='AppID 无效，拒绝启动。')
    url = PROTOCOL_RUN % value
    if not find_steam_dir():
        return dict(ok=False, error='这台电脑上没找到 Steam 客户端。')
    try:
        os.startfile(url)  # noqa: S606 - 只接受本机 manifest 里的数字 AppID
    except OSError as exc:
        return dict(ok=False, error='系统没能调起 Steam 协议：%s' % exc, appid=value, url=url)
    label = ('《%s》' % name) if name else ('AppID %d' % value)
    return dict(ok=True, appid=value, url=url, name=name,
                message='已请求 Steam 启动%s。游戏加载需要一点时间；'
                        '如果没反应，可能是需要更新、未登录或该游戏要求其它启动器。' % label)


# ---------------------------------------------------------------------------
# 命令行自检
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import sys
    words = list(argv if argv is not None else sys.argv[1:])
    steam_dir = find_steam_dir()
    print('Steam 安装目录：', steam_dir or '(未找到)')
    if not steam_dir:
        return 1
    games = installed_games(force=True)
    print('已安装游戏：%d 个' % len(games))
    for game in games:
        print('  %-8s %-46s %s' % (game['appid'], game['name'][:45],
                                   '已就绪' if game['ready'] else '需更新/未完整'))
    if words:
        query = ' '.join(words)
        hit = match(query, games)
        print('\n匹配「%s」：' % query, hit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
