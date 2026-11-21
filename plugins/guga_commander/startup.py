# -*- coding: utf-8 -*-
"""启动时的本机文件权限询问（只问第一次）。

桌宠第一次启动时问一次「能不能读取本机文件」，问完就记住：

* 选「允许访问」→ 完整访问打开，可以说「打开 D:\\报告.pdf」「打开桌面上的合同」；
* 选「以后再说」→ 完整访问关掉，插件拒绝读写，连项目自带的文件工具也一起关闭；
* 之后再启动**不再打扰**，用户想改就按弹窗里写的位置去开
  （对话窗口右上角「···」→「软件权限…」），或者直接对我说「允许读取文件」。

弹窗用 ``QTimer.singleShot`` 延迟 0.7 秒弹出，等桌宠窗口先画出来，不阻塞启动；
外观是一张无边框圆角卡片，默认焦点不落在「允许」上——手滑按回车不会误授权。
"""
from __future__ import annotations

try:
    from . import consent
except ImportError:  # 单文件调试
    import consent

TITLE = '咕嘎 · 本机文件权限'
HEADLINE = '允许咕嘎访问本机文件吗？'
BODY = '允许后可以直接帮你打开电脑上的文件和软件。'
FOOTNOTE = ('只问这一次 · 想改就点对话窗口右上角「···」'
            '→「软件权限…」')

CARD_WIDTH = 420

STYLE = '''
QFrame#Card {
    background: #fbfdfe;
    border: 1px solid #d6e2ea;
    border-radius: 16px;
}
QLabel#Head {
    color: #152934;
    font-family: "Microsoft YaHei UI";
    font-size: 15px;
    font-weight: 600;
}
QLabel#Body {
    color: #5a7080;
    font-family: "Microsoft YaHei UI";
    font-size: 12px;
}
QLabel#Foot {
    color: #8b9ca8;
    font-family: "Microsoft YaHei UI";
    font-size: 11px;
}
QFrame#Rule {
    background: #e8f0f4;
    border: 0;
}
QPushButton {
    font-family: "Microsoft YaHei UI";
    font-size: 12px;
    border-radius: 8px;
    padding: 7px 16px;
}
QPushButton#Ghost {
    background: transparent;
    color: #5d7382;
    border: 0;
}
QPushButton#Ghost:hover {
    background: #edf4f8;
    color: #2b4c5b;
}
QPushButton#Primary {
    background: #215b68;
    color: #ffffff;
    border: 0;
    font-weight: 600;
}
QPushButton#Primary:hover { background: #184a57; }
QPushButton#Primary:pressed { background: #123c47; }
'''


def _build_dialog(parent=None):
    """搭出那张卡片；没有 Qt 就返回 None。"""
    from PyQt5.QtCore import QRectF, Qt
    from PyQt5.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                             QPen)
    from PyQt5.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect,
                                 QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                                 QWidget)

    class Badge(QWidget):
        """手绘徽标：圆角渐变底 + 白色小锁，比字形徽标更稳。"""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setFixedSize(44, 44)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            gradient = QLinearGradient(0, 0, self.width(), self.height())
            gradient.setColorAt(0, QColor('#3d8a98'))
            gradient.setColorAt(1, QColor('#1b4e5b'))
            shape = QPainterPath()
            shape.addRoundedRect(QRectF(0, 0, self.width(), self.height()),
                                 12, 12)
            painter.fillPath(shape, gradient)

            white = QColor(255, 255, 255, 238)
            painter.setPen(Qt.NoPen)
            painter.setBrush(white)
            painter.drawRoundedRect(QRectF(13, 20, 18, 13), 3.5, 3.5)

            shackle = QPen(white)
            shackle.setWidthF(2.5)
            shackle.setCapStyle(Qt.RoundCap)
            painter.setPen(shackle)
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(QRectF(16, 12, 12, 13), 0, 180 * 16)
            painter.end()

    class PermissionDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.choice = 'deny'
            self._drag_from = None
            self.setWindowTitle(TITLE)
            # 无边框 + 透明底，才能露出圆角和投影。
            self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint
                                | Qt.WindowStaysOnTopHint)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setStyleSheet(STYLE)

            shell = QVBoxLayout(self)
            # 外面留白是给投影的，卡片本身不带边距。
            shell.setContentsMargins(22, 18, 22, 26)
            shell.setSpacing(0)

            card = QFrame(self)
            card.setObjectName('Card')
            card.setAttribute(Qt.WA_StyledBackground, True)
            card.setFixedWidth(CARD_WIDTH)
            shell.addWidget(card)

            shadow = QGraphicsDropShadowEffect(card)
            shadow.setBlurRadius(34)
            shadow.setColor(QColor(22, 45, 58, 58))
            shadow.setOffset(0, 7)
            card.setGraphicsEffect(shadow)

            body = QVBoxLayout(card)
            body.setContentsMargins(22, 20, 22, 16)
            body.setSpacing(0)

            head_row = QHBoxLayout()
            head_row.setSpacing(13)

            badge = Badge(card)
            head_row.addWidget(badge, 0, Qt.AlignTop)

            words = QVBoxLayout()
            words.setSpacing(5)
            head = QLabel(HEADLINE, card)
            head.setObjectName('Head')
            words.addWidget(head)
            desc = QLabel(BODY, card)
            desc.setObjectName('Body')
            desc.setWordWrap(True)
            words.addWidget(desc)
            head_row.addLayout(words, 1)
            body.addLayout(head_row)

            body.addSpacing(15)
            rule = QFrame(card)
            rule.setObjectName('Rule')
            rule.setFixedHeight(1)
            body.addWidget(rule)
            body.addSpacing(13)

            foot = QLabel(FOOTNOTE, card)
            foot.setObjectName('Foot')
            foot.setWordWrap(True)
            body.addWidget(foot)

            body.addSpacing(16)
            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            buttons.addStretch(1)

            deny = QPushButton('以后再说', card)
            deny.setObjectName('Ghost')
            deny.setCursor(Qt.PointingHandCursor)
            deny.setAutoDefault(False)
            deny.setDefault(False)
            deny.clicked.connect(self._deny)
            buttons.addWidget(deny)

            allow = QPushButton('允许访问', card)
            allow.setObjectName('Primary')
            allow.setCursor(Qt.PointingHandCursor)
            allow.setAutoDefault(False)
            allow.setDefault(False)
            allow.clicked.connect(self._allow)
            buttons.addWidget(allow)
            body.addLayout(buttons)

            self.adjustSize()

        # —— 选择 ——
        def _allow(self):
            self.choice = 'allow'
            self.accept()

        def _deny(self):
            self.choice = 'deny'
            self.reject()

        def keyPressEvent(self, event):  # 回车不授权，Esc 等于「以后再说」
            if event.key() == Qt.Key_Escape:
                self._deny()
                return
            super().keyPressEvent(event)

        # —— 无边框窗口拖动 ——
        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                self._drag_from = event.globalPos() - self.pos()
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._drag_from is not None and event.buttons() & Qt.LeftButton:
                self.move(event.globalPos() - self._drag_from)
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            self._drag_from = None
            super().mouseReleaseEvent(event)

        def center_on_screen(self):
            """桌宠平时贴在角落，卡片居中到屏幕上更好看。"""
            from PyQt5.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen is None:
                return
            area = screen.availableGeometry()
            self.move(area.center().x() - self.width() // 2,
                      area.center().y() - self.height() // 2 - 40)

    return PermissionDialog(parent)


def ask_now(parent=None):
    """弹一次卡片并写入结果。返回 True/False；不该问或没有图形环境时返回 None。"""
    if not consent.should_ask():
        return None  # 问过了就安静启动，位置写在弹窗和权限提示里
    try:
        from PyQt5.QtWidgets import QApplication
    except Exception:  # noqa: BLE001 - 没有 Qt（测试/命令行）就不打扰
        return None
    if QApplication.instance() is None:
        return None
    try:
        dialog = _build_dialog(parent)
        dialog.center_on_screen()
    except Exception as exc:  # noqa: BLE001 - 弹窗坏了也不能挡住桌宠
        print('[guga-commander] 启动授权弹窗构建失败：%s: %s'
              % (type(exc).__name__, exc))
        return None
    dialog.exec_()

    granted = dialog.choice == 'allow'
    consent.set_granted(granted, source='startup')
    consent.mark_asked('allow' if granted else 'deny')
    try:
        print('[guga-commander] 本机文件权限：%s' % ('已允许' if granted else '已拒绝'))
    except Exception:  # noqa: BLE001
        pass
    return granted


def preview():
    """只看外观：弹一次卡片，不写任何权限记录。"""
    try:
        from PyQt5.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        print('没有安装 PyQt5，无法预览')
        return None
    app = QApplication.instance() or QApplication([])
    dialog = _build_dialog(None)
    dialog.center_on_screen()
    dialog.exec_()
    print('预览结束，未修改权限（选择：%s）' % dialog.choice)
    return dialog.choice


if __name__ == '__main__':
    import sys
    preview() if '--preview' in sys.argv else print('用法：python -m plugins.guga_commander.startup --preview')


def ask_at_startup(window=None):
    """给 ``main.py`` 里的 QTimer 调用：任何异常都不许影响桌宠启动。"""
    try:
        return ask_now(window)
    except Exception as exc:  # noqa: BLE001
        try:
            print('[guga-commander] 启动授权弹窗失败：%s: %s'
                  % (type(exc).__name__, exc))
        except Exception:  # noqa: BLE001
            pass
        return None
