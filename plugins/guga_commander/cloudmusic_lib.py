# -*- coding: utf-8 -*-
"""直接操作网易云音乐**客户端**：在里面搜索、播放、把结果加进「我喜欢的音乐」。

为什么不用 UIA
--------------
客户端 3.x 主窗口是 CEF 宿主（窗口类 ``OrpheusBrowserHost``），整棵网页控件树
不暴露给 UI Automation，宿主的 ``uia`` 只能看到窗口本身一个节点。所以这条路
走的是**真实键鼠**：点搜索框 → 粘贴 → 回车 → 悬浮扫出结果行 → 点行内 ♥。

判定成功只认一条证据：**那颗 ♥ 从描边变成实心红**（取色比对）。点完没变红就说
「没确认到」，把窗口留在结果页让用户自己点——宁可不好听，也不谎报。

已知边界
--------
* 坐标是 3.1.x 的网页排版（CSS 像素，按窗口 DPI 换算）。换大版本可能位移；
  扫描找不到结果行会直接说「没认出来」，**不会盲点一通**。
* 悬浮扫描靠「结果行右侧的 ♥/播放/更多」三个图标亮起来判断行位置，所以
  必须先让窗口在前台、不能被别的窗口盖住。
* 全流程属于「向其它程序发送键鼠」，需要「完整访问」；没授权直接拒绝。
"""
from __future__ import annotations

import ctypes
import importlib
import sys
import time
from pathlib import Path

try:
    from . import app_lib, consent
except ImportError:  # 单文件调试
    import app_lib
    import consent

PROCESS = 'cloudmusic.exe'
WINDOW_CLASS = 'OrpheusBrowserHost'
APP_NAME = '网易云音乐'

# ---- 客户端 3.1.x 的网页排版（CSS 像素，相对窗口左上角）-------------------
CSS_SEARCH_BOX = (465, 37)      # 顶部搜索框
CSS_ROW_LIKE_X = 799            # 结果行悬浮出的三个图标中，♥ 的中心
CSS_ROW_PLAY_X = 752            # 三图标里的「播放」
CSS_SCAN_TOP = 300              # 结果行可能出现的纵向范围
CSS_SCAN_BOTTOM = 900
CSS_SCAN_STEP = 16
CSS_ICON_BAND = (748, 852)      # 判断「行是否被悬浮」的取样横带
CSS_PLAYBAR_TITLE = (80, 895, 330, 945)   # 底部播放条的歌名区域

BRIGHT = 120        # 图标是浅灰/白，背景近黑
RED_MIN = 25        # 判定「已喜欢」的红色像素下限
CONSENT_HINT = ('要在网易云客户端里点按钮，需要先开启「完整访问」。\n'
                '开启方式：点对话窗口右上角「···」→「软件权限…」，'
                '勾选「允许咕嘎完整访问本机」。')

user32 = ctypes.windll.user32


def _dpi_ready() -> None:
    """尽量让本进程按物理像素工作；已经在 Qt 里设过就随它去。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - 已设置/老系统都会抛，忽略
        pass


_dpi_ready()


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

def _core(name: str):
    """按脚本方式直接运行时，项目根目录不在 sys.path 上，补一次再导。"""
    try:
        return importlib.import_module(name)
    except ImportError:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module(name)


def find_window() -> dict:
    """找客户端主窗口；没开返回空 dict。"""
    try:
        windows = _core('core.window_inventory').list_windows()
    except Exception:  # noqa: BLE001 - 取窗口失败当作没开
        return {}
    for win in windows:
        if (win.get('process_name') or '').lower() != PROCESS:
            continue
        if win.get('window_class') != WINDOW_CLASS:
            continue
        if not win.get('hwnd'):
            continue
        return win
    return {}


def app_path() -> str:
    hit = app_lib.resolve(APP_NAME)
    if not hit.get('ok'):
        hit = app_lib.resolve('cloudmusic')
    return hit.get('path') or ''


def status() -> dict:
    win = find_window()
    return dict(running=bool(win), hwnd=win.get('hwnd', 0),
                title=win.get('title') or '', path=app_path())


def open_client(wait: float = 12.0) -> dict:
    """启动或唤起客户端，返回 ``{ok, hwnd, launched, message/error}``。"""
    win = find_window()
    if win:
        frame = _focus(win['hwnd'])
        return dict(ok=bool(frame), hwnd=win.get('hwnd', 0), launched=False,
                    message='网易云音乐已经在运行，已经切到前台。' if frame
                    else '网易云音乐开着，但没能切到前台。')
    path = app_path()
    if not path:
        return dict(ok=False, error='没在本机找到网易云音乐客户端（cloudmusic.exe）。')
    try:
        import os
        os.startfile(path)  # noqa: S606 - 就是「请 Windows 启动」
    except OSError as exc:
        return dict(ok=False, error='启动网易云音乐失败：%s' % exc)
    deadline = time.monotonic() + max(2.0, wait)
    while time.monotonic() < deadline:
        time.sleep(0.8)
        win = find_window()
        if win:
            frame = _focus(win['hwnd'])
            return dict(ok=bool(frame), hwnd=win.get('hwnd', 0), launched=True,
                        message='已启动网易云音乐。' if frame else '客户端起来了，还没能切到前台。')
    return dict(ok=False, error='等了一会儿还没看到网易云音乐窗口，可能启动得比较慢。')


# ---------------------------------------------------------------------------
# 坐标换算：CSS 像素 → 本进程坐标 → 屏幕物理像素
# ---------------------------------------------------------------------------

def _window_rect(hwnd: int):
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _dpi_scale(hwnd: int) -> float:
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi:
            return float(dpi) / 96.0
    except Exception:  # noqa: BLE001 - 老系统没这个 API
        pass
    try:
        dc = user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        user32.ReleaseDC(0, dc)
        return float(dpi) / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def _phys_ratio() -> float:
    """本进程坐标 → 屏幕物理像素的倍数（非 DPI 感知进程是 1.5，感知进程是 1.0）。"""
    try:
        from PIL import ImageGrab
        physical = ImageGrab.grab(all_screens=True).size[0]
        logical = user32.GetSystemMetrics(0)
        if logical:
            return max(1.0, physical / float(logical))
    except Exception:  # noqa: BLE001
        pass
    return 1.0


class Frame:
    """一次操作期间的窗口坐标换算。"""

    def __init__(self, hwnd: int):
        self.hwnd = int(hwnd)
        self.left, self.top, self.right, self.bottom = _window_rect(self.hwnd)
        self.dpi = _dpi_scale(self.hwnd)
        self.ratio = _phys_ratio()
        if self.ratio > 1.05:          # 本进程坐标是逻辑像素（= CSS 像素）
            self.coord_per_css, self.phys_per_coord = 1.0, self.ratio
        else:                          # 本进程坐标已经是物理像素
            self.coord_per_css, self.phys_per_coord = self.dpi, 1.0

    @property
    def width(self) -> int:
        return int((self.right - self.left) / self.coord_per_css)

    def point(self, css_x: float, css_y: float):
        return (int(round(self.left + css_x * self.coord_per_css)),
                int(round(self.top + css_y * self.coord_per_css)))

    def box(self, css_x0: float, css_y0: float, css_x1: float, css_y1: float):
        p = self.phys_per_coord
        x0, y0 = self.point(css_x0, css_y0)
        x1, y1 = self.point(css_x1, css_y1)
        return (int(x0 * p), int(y0 * p), int(x1 * p), int(y1 * p))

    def grab(self, css_x0: float, css_y0: float, css_x1: float, css_y1: float):
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=self.box(css_x0, css_y0, css_x1, css_y1),
                              all_screens=True).convert('RGB')


def _foreground_is(hwnd: int) -> bool:
    """前台窗口是不是这个进程的：取色靠屏幕像素，被别的窗口盖住就会读错。"""
    foreground = user32.GetForegroundWindow()
    if not foreground:
        return False
    if foreground == hwnd:
        return True
    try:
        mine, theirs = ctypes.c_ulong(), ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(mine))
        user32.GetWindowThreadProcessId(foreground, ctypes.byref(theirs))
        return bool(mine.value) and mine.value == theirs.value   # CEF 的渲染子窗口也算
    except Exception:  # noqa: BLE001
        return False


def _focus(hwnd: int):
    """把窗口拉到前台；真的到了前台才返回 Frame。"""
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)     # SW_RESTORE
        _attach_focus = _core('core.computer_control')._attach_focus
        focused = _attach_focus(hwnd)
        time.sleep(0.45)
        if not focused:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.35)
        if not _foreground_is(hwnd):
            return None                    # 盖在下面就别乱点，宁可如实说不成功
        return Frame(hwnd)
    except Exception:  # noqa: BLE001 - 切不了前台就没法操作
        return None


# ---------------------------------------------------------------------------
# 键鼠
# ---------------------------------------------------------------------------

def _move(frame: Frame, css_x: float, css_y: float) -> None:
    user32.SetCursorPos(*frame.point(css_x, css_y))


def _click(frame: Frame, css_x: float, css_y: float, double: bool = False,
           settle: float = 0.2) -> None:
    _move(frame, css_x, css_y)
    time.sleep(settle)                     # 先让悬浮图标亮起来再点
    user32.mouse_event(2, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(4, 0, 0, 0, 0)
    if double:
        time.sleep(0.06)
        user32.mouse_event(2, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(4, 0, 0, 0, 0)


def _keys(values) -> None:
    _core('core.computer_control')._send_keys(values)


def _paste(text: str) -> bool:
    """中文只能走剪贴板：先备份，粘贴完恢复，别动用户的剪贴板。"""
    try:
        import win32clipboard
    except ImportError:
        return False
    backup = None
    try:
        win32clipboard.OpenClipboard()
        try:
            backup = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        except Exception:  # noqa: BLE001 - 剪贴板里不是文本
            backup = None
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(str(text), win32clipboard.CF_UNICODETEXT)
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass
    _keys([17, 86])                        # ctrl+v
    time.sleep(0.25)
    if backup:
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(backup, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001 - 恢复失败不影响本次操作
            pass
    return True


# ---------------------------------------------------------------------------
# 观测
# ---------------------------------------------------------------------------

def _bright(frame: Frame, css_y: float) -> int:
    """行右侧图标带里的亮点数：悬浮在结果行上时才会亮。"""
    x0, x1 = CSS_ICON_BAND
    image = frame.grab(x0, css_y - 9, x1, css_y + 9)
    gray = image.convert('L')
    return sum(1 for value in gray.get_flattened_data() if value > BRIGHT)


def _red(frame: Frame, css_y: float, css_x: float = CSS_ROW_LIKE_X) -> int:
    """♥ 周围的红色像素数：实心红=已喜欢，描边=没喜欢。"""
    image = frame.grab(css_x - 16, css_y - 13, css_x + 16, css_y + 13)
    red = 0
    for r, g, b in image.get_flattened_data():
        if r > 150 and r > g * 1.5 and r > b * 1.5:
            red += 1
    return red


def _same(frame: Frame, before) -> bool:
    """对比两帧（播放条歌名区域）是否没变。"""
    try:
        after = frame.grab(*CSS_PLAYBAR_TITLE)
        if before is None or after.size != before.size:
            return False
        total = 0
        for (r1, g1, b1), (r2, g2, b2) in zip(after.get_flattened_data(),
                                              before.get_flattened_data()):
            total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
            if total > 6000:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def find_first_row(frame: Frame) -> int:
    """悬浮扫描出第一条结果行的中心 y（CSS 像素）；找不到返回 0。"""
    y = CSS_SCAN_TOP
    hit = 0
    while y <= CSS_SCAN_BOTTOM:
        _move(frame, CSS_ROW_LIKE_X, y)
        time.sleep(0.06)
        if _bright(frame, y) >= 12:
            hit = y
            break
        y += CSS_SCAN_STEP
    if not hit:
        return 0

    top = hit
    while top - 6 >= CSS_SCAN_TOP:
        _move(frame, CSS_ROW_LIKE_X, top - 6)
        time.sleep(0.05)
        if _bright(frame, top - 6) < 12:
            break
        top -= 6
    bottom = hit
    while bottom + 6 <= CSS_SCAN_BOTTOM:
        _move(frame, CSS_ROW_LIKE_X, bottom + 6)
        time.sleep(0.05)
        if _bright(frame, bottom + 6) < 12:
            break
        bottom += 6
    return (top + bottom) // 2


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------

def stable_row(frame: Frame, tries: int = 4, gap: float = 0.45) -> int:
    """连续两次扫到同一行才算数：搜索结果页刚渲染完时整列会跳一下，
    照着旧位置点很容易点到上一行/空档（实测踩过）。"""
    last = 0
    for _ in range(max(2, tries)):
        row = find_first_row(frame)
        if row and last and abs(row - last) <= 8:
            return row
        last = row
        if row:
            time.sleep(gap)
    return last


def _type_query(frame: Frame, text: str) -> dict:
    _click(frame, *CSS_SEARCH_BOX, settle=0.12)
    time.sleep(0.3)
    _keys([17, 65])                     # ctrl+a 覆盖原来的词
    time.sleep(0.12)
    if not _paste(text):
        return dict(ok=False, error='剪贴板不可用，没法输入中文歌名。')
    time.sleep(0.2)
    _keys([13])                         # 回车搜索
    time.sleep(1.6)
    return dict(ok=True)


def _search_only(query: str) -> dict:
    text = str(query or '').strip()
    if not text:
        return dict(ok=False, code='empty_query', error='要搜什么歌？')
    if not consent.is_granted():
        return dict(ok=False, code='no_consent', error=CONSENT_HINT)
    ready = open_client()
    if not ready.get('ok'):
        return dict(ok=False, code='no_client',
                    error=ready.get('error') or '没能把网易云音乐拉到前台。')
    frame = Frame(ready['hwnd'])
    typed = _type_query(frame, text)
    if not typed.get('ok'):
        return dict(ok=False, code='input_failed', error=typed.get('error') or '没能输入歌名。')
    return dict(ok=True, frame=frame, hwnd=frame.hwnd, query=text)


def search(query: str) -> dict:
    """在客户端里搜，停在结果页。"""
    done = _search_only(query)
    if not done.get('ok'):
        return done
    return dict(ok=True, hwnd=done['hwnd'], query=done['query'],
                message='已在网易云音乐里搜「%s」，结果就在窗口里。' % done['query'])


def play(query: str) -> dict:
    """在客户端里搜，并播放第一条。"""
    done = _search_only(query)
    if not done.get('ok'):
        return done
    frame = done['frame']
    before = frame.grab(*CSS_PLAYBAR_TITLE)
    row = stable_row(frame)
    if not row:
        return dict(ok=False, code='ui_not_found', hwnd=frame.hwnd, query=done['query'],
                    error='没在客户端里认出搜索结果行（界面可能变了）。\n'
                          '已经帮你停在搜索结果页，点一下就能播。')
    _click(frame, CSS_ROW_PLAY_X, row, settle=0.25)
    time.sleep(1.2)
    changed = not _same(frame, before)
    return dict(ok=changed, verified=changed, hwnd=frame.hwnd, query=done['query'],
                row=row,
                message=('已在网易云音乐播放搜索结果第一条（搜索词：%s）。'
                         % done['query']) if changed else '',
                error='' if changed else '点了播放，但播放条没变化，可能这一行不是歌曲。')


def like(query: str, song: str = '') -> dict:
    """在客户端里搜一首歌，把**第一条结果**加进「我喜欢的音乐」。

    ``song`` 只用来在回执里点名；真正的操作对象永远是搜索结果第一条。
    """
    done = _search_only(query)
    if not done.get('ok'):
        return done
    frame = done['frame']
    row = stable_row(frame)
    if not row:
        return dict(ok=False, code='ui_not_found', hwnd=frame.hwnd, query=done['query'],
                    error='没在客户端里认出搜索结果行（界面可能变了）。\n'
                          '已经帮你停在搜索结果页，点一下那颗 ♥ 就行。')

    if _red(frame, row) >= RED_MIN:
        return dict(ok=True, verified=True, already=True, hwnd=frame.hwnd, row=row,
                    message=('搜索结果第一条%s本来就在「我喜欢的音乐」里。'
                             % (('《%s》' % song) if song else '')))

    _click(frame, CSS_ROW_LIKE_X, row, settle=0.25)
    verified = False
    for _ in range(3):
        time.sleep(0.5)
        # 页面会重排：既看原点，也重新扫一次当前位置
        moved = stable_row(frame, tries=2, gap=0.3)
        if _red(frame, row) >= RED_MIN or (moved and _red(frame, moved) >= RED_MIN):
            verified = True
            break
    steps = [dict(step='搜索', ok=True, detail=done['query']),
             dict(step='找到第一条结果', ok=True, detail='第 %d 像素行' % row),
             dict(step='点红心', ok=True, detail='已点击'),
             dict(step='确认收藏', ok=verified,
                  detail='红心已变红' if verified else '红心没变红')]
    if verified:
        return dict(ok=True, verified=True, hwnd=frame.hwnd, row=row, steps=steps,
                    message=('已把搜索结果第一条%s加进「我喜欢的音乐」'
                             '（在网易云客户端里点的 ♥）。'
                             % (('《%s》' % song) if song else '')))
    return dict(ok=False, code='unverified', hwnd=frame.hwnd, row=row, steps=steps,
                action_sent=True,
                error='点了那颗 ♥，但没确认到它变红（可能没登录，或这一行不是歌曲）。\n'
                      '客户端停在搜索结果页，你点一下就能确认。')


if __name__ == '__main__':  # python cloudmusic_lib.py "稻香" [play|like]
    import sys
    args = sys.argv[1:]
    if not args:
        print('用法：python plugins/guga_commander/cloudmusic_lib.py "稻香" [play|like]')
        print(status())
        raise SystemExit(0)
    action = args[1] if len(args) > 1 else 'search'
    func = dict(search=search, play=play, like=like).get(action, search)
    print(func(args[0]))
