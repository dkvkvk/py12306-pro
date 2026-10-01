"""界面主题：深色 + 状态色（与网页面板同一套配色，保持两处观感一致）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# 状态色（熔断/风控语义与网页面板一致）
COLOR_OK = "#3fb950"       # 正常
COLOR_WARN = "#d29922"     # 退避/注意
COLOR_BAD = "#f85149"      # 熔断/风控
COLOR_INFO = "#58a6ff"     # 信息
COLOR_ACCENT = "#a371f7"

COLOR_BG = "#0d1117"
COLOR_BG_SOFT = "#161b22"
COLOR_LINE = "#21262d"
COLOR_FG = "#e6edf3"
COLOR_FG_DIM = "#8b949e"

#: 熔断状态 -> 颜色
STATE_COLORS = {
    "closed": COLOR_OK,
    "backoff": COLOR_WARN,
    "open": COLOR_BAD,
    "half_open": COLOR_INFO,
}

STYLESHEET = """
QWidget { background: %(bg)s; color: %(fg)s; font-size: 13px; }
QMainWindow::separator { background: %(line)s; }
QTabWidget::pane { border: 1px solid %(line)s; border-radius: 8px; top: -1px; }
QTabBar::tab {
    background: %(bg_soft)s; color: %(fg_dim)s; padding: 7px 16px;
    border: 1px solid %(line)s; border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 2px;
}
QTabBar::tab:selected { background: %(bg)s; color: %(fg)s; border-bottom: 2px solid %(info)s; }
QGroupBox {
    border: 1px solid %(line)s; border-radius: 8px; margin-top: 12px; padding: 10px 12px 12px 12px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: %(fg_dim)s; }
QPushButton {
    background: #21262d; color: %(fg)s; border: 1px solid #30363d;
    border-radius: 6px; padding: 6px 14px; min-height: 20px;
}
QPushButton:hover { background: #30363d; }
QPushButton:disabled { color: %(fg_dim)s; background: #1b1f24; }
QPushButton#primary { background: #1f6feb; border-color: #1f6feb; }
QPushButton#primary:hover { background: #388bfd; }
QPushButton#danger { background: #3d1418; border-color: #6e2429; color: #ffb3ae; }
QPushButton#danger:hover { background: #5c1b21; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: #0b0f14; border: 1px solid #30363d; border-radius: 6px; padding: 5px 8px;
    selection-background-color: #1f6feb;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: %(info)s; }
QCheckBox::indicator { width: 15px; height: 15px; }
QTableView, QTableWidget {
    background: #0b0f14; alternate-background-color: #11161c;
    gridline-color: %(line)s; border: 1px solid %(line)s; border-radius: 8px;
    selection-background-color: #1f6feb; selection-color: #ffffff;
}
QHeaderView::section {
    background: %(bg_soft)s; color: %(fg_dim)s; padding: 6px 8px;
    border: none; border-right: 1px solid %(line)s; border-bottom: 1px solid %(line)s;
}
QScrollBar:vertical { background: %(bg)s; width: 11px; margin: 0; }
QScrollBar::handle:vertical { background: #30363d; border-radius: 5px; min-height: 26px; }
QScrollBar::handle:vertical:hover { background: #484f58; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: %(bg)s; height: 11px; }
QScrollBar::handle:horizontal { background: #30363d; border-radius: 5px; min-width: 26px; }
QStatusBar { background: %(bg_soft)s; color: %(fg_dim)s; border-top: 1px solid %(line)s; }
QToolTip { background: %(bg_soft)s; color: %(fg)s; border: 1px solid %(line)s; padding: 4px 6px; }
QMenuBar, QMenu { background: %(bg_soft)s; color: %(fg)s; }
QMenu::item:selected { background: %(info)s; color: #ffffff; }
QSplitter::handle { background: %(line)s; }
""" % {
    "bg": COLOR_BG,
    "bg_soft": COLOR_BG_SOFT,
    "line": COLOR_LINE,
    "fg": COLOR_FG,
    "fg_dim": COLOR_FG_DIM,
    "info": COLOR_INFO,
}

#: 界面首选字体（Windows 上优先中文友好的字体）
FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
    "Noto Sans CJK SC",
    "PingFang SC",
    "WenQuanYi Micro Hei",
    "sans-serif",
)


def state_color(state: str) -> str:
    return STATE_COLORS.get(str(state or "").lower(), COLOR_FG_DIM)


def pick_font() -> QFont:
    """挑一个系统里真实存在的中文字体。

    Qt 6 不再自带字体；打包环境里如果没有中文字体，界面会显示成方块。
    这里按候选列表探测，挑不到就退回系统默认。
    """
    from PySide6.QtGui import QFontDatabase

    families = set(QFontDatabase.families())
    for name in FONT_CANDIDATES:
        if name in families:
            font = QFont(name)
            font.setPointSize(10)
            return font
    font = QFont()
    font.setPointSize(10)
    return font


def apply_theme(app: QApplication) -> None:
    """设置调色板 + 样式表 + 字体。"""
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(COLOR_BG))
    palette.setColor(QPalette.WindowText, QColor(COLOR_FG))
    palette.setColor(QPalette.Base, QColor("#0b0f14"))
    palette.setColor(QPalette.AlternateBase, QColor(COLOR_BG_SOFT))
    palette.setColor(QPalette.Text, QColor(COLOR_FG))
    palette.setColor(QPalette.Button, QColor("#21262d"))
    palette.setColor(QPalette.ButtonText, QColor(COLOR_FG))
    palette.setColor(QPalette.Highlight, QColor(COLOR_INFO))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, QColor(COLOR_BG_SOFT))
    palette.setColor(QPalette.ToolTipText, QColor(COLOR_FG))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(COLOR_FG_DIM))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(COLOR_FG_DIM))
    app.setPalette(palette)

    app.setStyleSheet(STYLESHEET)
    app.setFont(pick_font())
