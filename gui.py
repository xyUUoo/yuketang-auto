# -*- coding: utf-8 -*-
"""
雨课堂刷课控制台（PySide6 / 深色分层卡片）
================================================
- 进度看板：表格展示各课程 总目标/剩余/完成度进度条/状态/类型分布
- 实时日志：只读日志区，增量追加、停在底部才自动跟随
- 一键控制：开始刷课（完整刷/快速检测 可选）/ 暂停·继续 / 结束（含清理残留 Edge）/ 检查状态 / 刷新
- 视觉层：token 化配色 + 全局 QSS + objectName 选择器，无内联样式

运行：双击 gui.bat 或 python gui.py
"""
import datetime
import json
import multiprocessing  # 仅用于 freeze_support（auto 内部并行 spawn）
import os
import subprocess
import sys
import threading
import time
import urllib.request

import auto  # 内嵌核心刷课逻辑（打包后随本程序一起分发）

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

# ---- 版本与更新检查 ----
APP_VERSION = "1.0.16"        # 每次发新版前手动递增
GITHUB_REPO = "xyUUoo/yuketang-auto"
GITHUB_RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_PY = os.path.join(BASE_DIR, "auto.py")
PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
PAUSE_FILE = os.path.join(BASE_DIR, "pause.flag")
IGNORE_FILE = os.path.join(BASE_DIR, "ignored_update.txt")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
DEFAULT_PROXY = "http://127.0.0.1:12450"

# 打包（PyInstaller）后：单文件双模式 —— 双击=GUI；GUI 内部用 subprocess 调自身
# （--auto-cli 参数）跑刷课逻辑，与 GUI 完全隔离，互不干扰
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BASE_DIR = os.path.dirname(sys.executable)
    PYTHON = None
    PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
    LOG_FILE = os.path.join(BASE_DIR, "log.txt")
    # 当前正在运行的程序本身：用户改过 exe 文件名也跟随（更新安装用这个名字替换/杀进程）
    APP_EXE = sys.executable
    APP_NAME = os.path.basename(sys.executable)
else:
    PYTHON = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
    APP_EXE = os.path.join(BASE_DIR, "yuketang-gui.exe")
    APP_NAME = "yuketang-gui.exe"

# CLI 模式标志：打包后 subprocess 调用自身时附加，入口据此直接跑刷课逻辑
CLI_FLAG = "--auto-cli"


def auto_cmd(extra_args):
    """构造调用 auto 的命令：源码环境用 venv python；打包后调自身（--auto-cli）"""
    if FROZEN:
        return [sys.executable, CLI_FLAG] + extra_args
    return [PYTHON, AUTO_PY] + extra_args


# ==============================================================================
# 第 1 层 · Design Token（深色 / 分层卡片 / 全部颜色与尺度的唯一来源）
# ==============================================================================
T_BG = "#1e1f26"            # 窗口底
T_BAR = "#232530"           # 标题条 / 控制条
T_SURFACE = "#262833"       # 表格 / 面板
T_SURFACE_ALT = "#2b2d3a"   # 斑马纹 / 输入控件
T_SUNKEN = "#14151b"        # 日志控制台
T_HEADER = "#2f3242"        # 表头
T_TRACK = "#33364a"         # 进度条轨道 / 分隔
T_BORDER = "#3a3d4d"        # 容器描边
T_BORDER_SUBTLE = "#333647"  # 细网格
T_TEXT_1 = "#e6e6e6"        # 正文（13.2:1）
T_TEXT_2 = "#c9cdd8"        # 表头 / 次要标题（7.9:1）
T_TEXT_3 = "#9aa0b4"        # 说明 / 元信息（6.3:1）
T_TEXT_4 = "#8a91a8"        # 占位 / 弱信息（5.2:1）
T_LOG_TEXT = "#b8c7dc"      # 日志正文（10.7:1）
T_ACCENT = "#5b6ee1"        # 主蓝（仅用于填充块）
T_ACCENT_HI = "#6c7fe8"
T_ACCENT_DEEP = "#4958b5"
T_ACCENT_TEXT = "#7f8ff5"   # 文字专用主蓝（5.57:1）
T_SUCCESS = "#2ea043"
T_SUCCESS_HI = "#34b44c"
T_SUCCESS_DEEP = "#257f35"
T_WARNING = "#d29922"
T_WARNING_HI = "#e0a632"
T_WARNING_DEEP = "#a87a18"
T_DANGER = "#c0392b"
T_DANGER_HI = "#d44535"
T_DANGER_DEEP = "#9a2c21"
T_SCROLL = "#4a4f68"
T_DIS_BG = "#22242e"
T_DIS_BORDER = "#2c2f3c"
T_DIS_TEXT = "#7d8496"
# 状态底纹（12~15% 透明度的 pill 背景）
T_SOFT_SUCCESS = "rgba(46, 160, 67, 38)"
T_SOFT_WARNING = "rgba(210, 153, 34, 38)"
T_SOFT_ACCENT = "rgba(91, 110, 225, 38)"
T_SOFT_DANGER = "rgba(192, 57, 43, 44)"
T_SOFT_NEUTRAL = "rgba(138, 145, 168, 30)"
T_SEL_ROW = "rgba(91, 110, 225, 60)"

# 字号阶梯：12 辅助 / 13 正文 / 14 状态 / 16 区块 / 18 标题（最小 12，不再用 11）
FS_AUX = 12
FS_BODY = 13
FS_STATE = 13
FS_SECTION = 16
FS_TITLE = 18
RAD_SM = 6
RAD_MD = 8
ROW_H = 44

# 类型展示优先级：视频最关心，其余依次；未列出的排在最后
TYPE_ORDER = ("视频", "课件", "作业", "讨论", "考试", "图文", "开放型作业")

# 表头：宽窗全称，窄窗用短词（窄列放不下）
COL_HEADERS = ["课程", "总目标", "剩余", "完成度", "状态", "类型分布（计分）"]
COL_HEADERS_NARROW = ["课程", "目标", "剩余", "完成度", "状态", "类型分布"]


def sorted_types(d):
    return sorted(
        d.items(),
        key=lambda kv: (TYPE_ORDER.index(kv[0]) if kv[0] in TYPE_ORDER else len(TYPE_ORDER), kv[0]),
    )

# ==============================================================================
# 第 4 层 · 状态机（文案与颜色只从这里取）
# ==============================================================================
STATUS = {
    "idle": ("空闲", T_TEXT_3, T_SOFT_NEUTRAL),
    "running": ("刷课中…", T_WARNING_HI, T_SOFT_WARNING),
    "checking": ("检查中…", T_ACCENT_TEXT, T_SOFT_ACCENT),
    "paused": ("已暂停", T_WARNING_HI, T_SOFT_WARNING),
    "stopped": ("已停止", T_TEXT_3, T_SOFT_NEUTRAL),
    "finished": ("运行结束", T_SUCCESS, T_SOFT_SUCCESS),
    "error": ("运行异常结束", T_DANGER_HI, T_SOFT_DANGER),
}

# 表格内单课程状态：文案 / 文字色 / 底纹(RGBA)
COURSE_STATUS = {
    "waiting": ("等待中", T_ACCENT_TEXT, (91, 110, 225, 34)),
    "running": ("刷课中", T_WARNING_HI, (210, 153, 34, 34)),
    "checking": ("检查中", T_ACCENT_TEXT, (91, 110, 225, 34)),
    "done": ("已刷完 ✓", T_SUCCESS, (46, 160, 67, 34)),
    "none": ("无任务", T_TEXT_4, (138, 145, 168, 26)),
}

# ==============================================================================
# 第 2/3 层 · 全局 QSS（按 token 拼装；状态色由 STATUS 表生成，避免第二处定义）
# ==============================================================================
_TOK = dict(
    bg=T_BG, bar=T_BAR, surface=T_SURFACE, surface_alt=T_SURFACE_ALT, sunken=T_SUNKEN,
    header=T_HEADER, track=T_TRACK, border=T_BORDER, border_subtle=T_BORDER_SUBTLE,
    t1=T_TEXT_1, t2=T_TEXT_2, t3=T_TEXT_3, t4=T_TEXT_4, log=T_LOG_TEXT,
    accent=T_ACCENT, accent_hi=T_ACCENT_HI, accent_deep=T_ACCENT_DEEP, accent_text=T_ACCENT_TEXT,
    success=T_SUCCESS, success_hi=T_SUCCESS_HI, success_deep=T_SUCCESS_DEEP,
    warning=T_WARNING, warning_hi=T_WARNING_HI, warning_deep=T_WARNING_DEEP,
    danger=T_DANGER, danger_hi=T_DANGER_HI, danger_deep=T_DANGER_DEEP,
    scroll=T_SCROLL, dis_bg=T_DIS_BG, dis_border=T_DIS_BORDER, dis_text=T_DIS_TEXT,
    sel_row=T_SEL_ROW,
)

_STATUS_QSS = "\n".join(
    'QLabel#statusLabel[state="%s"] { color: %s; background-color: %s; }' % (k, c, bg)
    for k, (_t, c, bg) in STATUS.items()
)

QSS = """
/* 基础 */
QWidget { color: %(t1)s; font-family: "Microsoft YaHei UI", "Microsoft YaHei"; font-size: 13px; }
QMainWindow { background-color: %(bg)s; }
QWidget#headBar { background-color: %(bar)s; border-bottom: 1px solid %(border)s; }
QWidget#ctrlBar { background-color: %(bar)s; border-top: 1px solid %(border)s; }

/* 文字层级 */
QLabel { background: transparent; }
QLabel#titleLabel { font-size: 18px; font-weight: 700; color: %(t1)s; }
QLabel#metaLabel { font-size: 13px; color: %(t3)s; }
QLabel#fieldLabel { font-size: 13px; color: %(t3)s; }
QLabel#emptyTitle { font-size: 16px; font-weight: 600; color: %(t2)s; }
QLabel#emptySub { font-size: 13px; color: %(t3)s; }
QLabel#statusLabel { font-size: 13px; font-weight: 600; padding: 4px 12px; border-radius: 11px; }
%(status_qss)s

/* 面板 */
QWidget#emptyPanel { background-color: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; }
QTableWidget {
    background-color: %(surface)s; alternate-background-color: %(surface_alt)s;
    border: 1px solid %(border)s; border-radius: 8px;
    gridline-color: %(border_subtle)s; font-size: 13px;
    selection-background-color: %(sel_row)s; selection-color: %(t1)s;
}
QTableWidget::item { padding: 0 6px; }
QTableWidget::item:selected { background-color: %(sel_row)s; color: %(t1)s; }
QHeaderView::section {
    background-color: %(header)s; color: %(t2)s; border: none;
    border-bottom: 1px solid %(border)s; padding: 8px 6px;
    font-size: 13px; font-weight: 600;
}

/* 完成度进度条（复用同一控件，只靠 full 属性切色） */
QProgressBar#courseBar {
    background-color: %(track)s; border: none; border-radius: 8px;
    text-align: center; color: #ffffff; font-size: 12px;
    min-height: 16px; max-height: 16px;
}
QProgressBar#courseBar::chunk { border-radius: 8px; background-color: %(accent)s; }
QProgressBar#courseBar[full="true"]::chunk { background-color: %(success)s; }
/* 完成度格第二行：分类型完成度小字（视频 2/21 · 课件 0/8） */
QLabel#barSub { color: %(t4)s; font-size: 11px; background: transparent; padding: 0; }

/* 日志 */
QPlainTextEdit {
    background-color: %(sunken)s; color: %(log)s;
    border: 1px solid %(border)s; border-radius: 8px;
    font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; padding: 8px;
}

/* 输入类 */
QLineEdit {
    background-color: %(surface_alt)s; color: %(t1)s;
    border: 1px solid %(border)s; border-radius: 6px; padding: 7px 10px; font-size: 13px;
}
QLineEdit:focus { border-color: %(accent)s; }
QComboBox {
    background-color: %(surface_alt)s; color: %(t1)s;
    border: 1px solid %(border)s; border-radius: 6px; padding: 6px 10px; font-size: 13px;
}
QComboBox:hover { border-color: %(accent)s; }
QComboBox:disabled { color: %(t4)s; background-color: %(dis_bg)s; border-color: %(dis_border)s; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: %(surface)s; color: %(t1)s; border: 1px solid %(border)s;
    selection-background-color: %(accent)s; selection-color: #ffffff;
}

/* 按钮四态（含 disabled，四类实心按钮各写一份以保证对比度） */
QPushButton {
    background-color: %(surface_alt)s; color: %(t1)s;
    border: 1px solid %(border)s; border-radius: 6px;
    padding: 7px 16px; font-size: 13px; font-weight: 500;
}
QPushButton:hover { background-color: %(track)s; }
QPushButton:pressed { background-color: #242633; }
QPushButton:disabled {
    background-color: %(dis_bg)s; color: %(dis_text)s; border-color: %(dis_border)s;
}
QPushButton#ghostBtn { background-color: transparent; color: %(t2)s; border: 1px solid %(border)s; }
QPushButton#ghostBtn:hover { background-color: %(track)s; color: %(t1)s; }
QPushButton#ghostBtn:pressed { background-color: #242633; }

QPushButton#startBtn { background-color: %(success)s; color: #ffffff; border: 1px solid %(success_deep)s; }
QPushButton#startBtn:hover { background-color: %(success_hi)s; }
QPushButton#startBtn:pressed { background-color: %(success_deep)s; }
QPushButton#startBtn:disabled { background-color: #22412c; color: #7fa48a; border-color: #2a4d33; }

QPushButton#pauseBtn { background-color: %(warning)s; color: #ffffff; border: 1px solid %(warning_deep)s; }
QPushButton#pauseBtn:hover { background-color: %(warning_hi)s; }
QPushButton#pauseBtn:pressed { background-color: %(warning_deep)s; }
QPushButton#pauseBtn:disabled { background-color: #453c22; color: #b3a077; border-color: #4d4426; }

QPushButton#stopBtn { background-color: %(danger)s; color: #ffffff; border: 1px solid %(danger_deep)s; }
QPushButton#stopBtn:hover { background-color: %(danger_hi)s; }
QPushButton#stopBtn:pressed { background-color: %(danger_deep)s; }
QPushButton#stopBtn:disabled { background-color: #452723; color: #b58a83; border-color: #4d2c26; }

QPushButton#primaryBtn { background-color: %(accent)s; color: #ffffff; border: 1px solid %(accent_deep)s; }
QPushButton#primaryBtn:hover { background-color: %(accent_hi)s; }
QPushButton#primaryBtn:pressed { background-color: %(accent_deep)s; }
QPushButton#primaryBtn:disabled { background-color: #2f3557; color: #8b93c4; border-color: #363d63; }

/* 分栏与滚动条 */
QSplitter::handle { background-color: transparent; width: 10px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 5px; min-height: 40px; }
QScrollBar::handle:vertical:hover { background: %(accent)s; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: %(scroll)s; border-radius: 5px; min-width: 40px; }
QScrollBar::handle:horizontal:hover { background: %(accent)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip {
    background-color: %(surface_alt)s; color: %(t1)s;
    border: 1px solid %(border)s; padding: 4px 8px;
}
""" % dict(_TOK, status_qss=_STATUS_QSS)


class RoundBar(QProgressBar):
    """圆角进度条（自绘）。

    直接靠 QSS 的 `::chunk { border-radius: 8px }` 时，进度很小时 chunk 总宽度
    不足圆角直径（bar 只有 16px 高），Qt 会把它退化成直角——左端看起来是方块
    （`5 / 46` 这种 11% 的进度就会这样）。这里改为自绘，圆角半径按 chunk 实际
    宽度收敛（半径 = min(8, 宽度/2)），窄进度也能保持圆头。
    """

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0

        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        p.fillPath(track, QColor(T_TRACK))

        span = self.maximum() - self.minimum()
        ratio = 0.0 if span <= 0 else (self.value() - self.minimum()) / float(span)
        ratio = max(0.0, min(1.0, ratio))
        w = rect.width() * ratio
        if ratio > 0:
            # 进度极小时真实宽度只剩几像素，圆角再大也只能画成一根细竖条（看着像直角）。
            # 给一个等于条高的最小宽度，视觉上变成一颗圆点；准确数值看条内的文字。
            w = min(rect.width(), max(w, rect.height()))
            chunk = QRectF(rect.x(), rect.y(), w, rect.height())
            r2 = min(radius, w / 2.0)
            path = QPainterPath()
            path.addRoundedRect(chunk, r2, r2)
            color = T_SUCCESS if self.property("full") else T_ACCENT
            p.fillPath(path, QColor(color))

        p.setPen(QColor("#ffffff"))
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self.text())


class ElideDelegate(QStyledItemDelegate):
    """课程名列：按当前单元格宽度省略，完整名称走 ToolTip（避免长名把列撑宽/抖动）"""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        w = opt.widget
        style = w.style() if w is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, w)
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, w).adjusted(6, 0, -6, 0)
        elided = opt.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.save()
        role = (QPalette.ColorRole.HighlightedText
                if (opt.state & QStyle.StateFlag.State_Selected) else QPalette.ColorRole.Text)
        painter.setPen(opt.palette.color(role))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)
        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(min(s.width(), 240), max(s.height(), ROW_H - 4))


class Console(QMainWindow):
    # 跨线程信号：后台检查线程发现新版本 → 主线程弹窗；下载线程 → 主线程更新进度/收尾
    update_found = Signal(str, str)
    upd_progress = Signal(int, int, int, float)   # pct, done, total, speed(MB/s)
    upd_finished = Signal(str)             # 下载完成，new_exe 路径
    upd_error = Signal(str)                # 下载失败原因
    log_note = Signal(str)                 # 后台线程 → 主线程日志

    def __init__(self):
        super().__init__()
        self.setWindowTitle("雨课堂刷课控制台")
        self.resize(1040, 720)
        self.setMinimumSize(900, 600)

        self.proc = None
        self.running_mode = ""
        self.status_key = ""
        self._manual_stop = False
        # 第 5 层：表格 diff 缓存与控件复用
        self._order = []            # 当前表格中课程名的顺序
        self._rows = {}             # name -> row
        self._holders = {}          # name -> 进度条容器（复用，不重建）
        self._cache = {}            # name -> 该行所有可变字段的快照
        # 第 6 层：日志增量读取状态
        self._log_offset = None
        self._log_inited = False

        self.update_found.connect(self._on_update_found)
        self.upd_progress.connect(self._upd_progress)
        self.upd_finished.connect(self._finish_update)
        self.upd_error.connect(self._upd_failed)
        self.log_note.connect(self.append_log_note)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ===== 标题条（分层卡片之一）=====
        head = QWidget()
        head.setObjectName("headBar")
        head.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        head.setFixedHeight(52)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 10, 16, 10)
        hl.setSpacing(12)
        title = QLabel("雨课堂刷课控制台")
        title.setObjectName("titleLabel")
        hl.addWidget(title)
        hl.addStretch(1)
        self.updated_lbl = QLabel("更新：—")
        self.updated_lbl.setObjectName("metaLabel")
        hl.addWidget(self.updated_lbl)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghostBtn")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self._open_settings)
        hl.addWidget(self.settings_btn)
        root.addWidget(head)

        # ===== 主体：表格(可切空态) + 日志 =====
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)

        self.table_stack = QStackedWidget()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(COL_HEADERS)
        # 文字列（课程、类型分布）表头跟内容一样左对齐：类型分布会随窗口变宽，
        # 居中的表头会跑得离内容很远。
        for _col in (0, 5):
            _it = self.table.horizontalHeaderItem(_col)
            if _it is not None:
                _it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_H)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        # 按像素平滑滚动，拖起来不卡手
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.table.horizontalHeader()
        # 列宽：课程列定宽可拖（超长走省略号 + ToolTip），类型分布吃剩余空间。
        # 反过来（课程列 Stretch、类型分布 Fixed）会让窗口放大后课程列独占空白、
        # 类型分布永远只有 96px 被截断。
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 156)
        for col, w in ((1, 52), (2, 46), (3, 142), (4, 76)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, w)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hh.setStretchLastSection(False)
        delegate = ElideDelegate(self.table)
        self.table.setItemDelegateForColumn(0, delegate)
        self.table.setItemDelegateForColumn(5, delegate)
        self.table_stack.addWidget(self.table)

        # 第 7 层 · 空态占位（progress.json 缺失/损坏时整页替换，不残留旧行）
        self.empty_panel = QWidget()
        self.empty_panel.setObjectName("emptyPanel")
        self.empty_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ep = QVBoxLayout(self.empty_panel)
        ep.setContentsMargins(40, 40, 40, 40)
        ep.setSpacing(12)
        ep.addStretch(1)
        self.empty_title = QLabel("尚无进度数据")
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_sub = QLabel("")
        self.empty_sub.setObjectName("emptySub")
        self.empty_sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_sub.setWordWrap(True)
        # 给固定宽度：自动换行下 QLabel 的 sizeHint 高度才按实际宽度算，否则末行被裁
        self.empty_sub.setFixedWidth(520)
        empty_btn = QPushButton("去检查状态")
        empty_btn.setObjectName("primaryBtn")
        empty_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        empty_btn.clicked.connect(self.check_status)
        ep.addWidget(self.empty_title, 0, Qt.AlignmentFlag.AlignHCenter)
        ep.addWidget(self.empty_sub, 0, Qt.AlignmentFlag.AlignHCenter)
        ep.addSpacing(4)
        ep.addWidget(empty_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        ep.addStretch(1)
        self.table_stack.addWidget(self.empty_panel)
        self.table_stack.setMinimumWidth(340)
        split.addWidget(self.table_stack)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setPlaceholderText("暂无日志输出")
        self.log_view.setMinimumWidth(300)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 6)
        split.setStretchFactor(1, 4)
        split.setSizes([620, 400])
        bl.addWidget(split)
        root.addWidget(body, 1)

        # ===== 控制条（分层卡片之三；窄窗自动换两行）=====
        self.ctrl = QWidget()
        self.ctrl.setObjectName("ctrlBar")
        self.ctrl.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.ctrl_grid = QGridLayout(self.ctrl)
        self.ctrl_grid.setContentsMargins(16, 8, 16, 8)
        self.ctrl_grid.setHorizontalSpacing(8)
        self.ctrl_grid.setVerticalSpacing(6)

        self.lbl_lessons = QLabel("每门条数：")
        self.lbl_lessons.setObjectName("fieldLabel")
        self.lessons_box = QComboBox()
        self.lessons_box.addItems(["全部", "1", "2", "3", "5", "10"])
        self.lessons_box.setCurrentIndex(0)
        self.lessons_box.setMinimumWidth(72)

        self.lbl_parallel = QLabel("并行数：")
        self.lbl_parallel.setObjectName("fieldLabel")
        self.parallel_box = QComboBox()
        self.parallel_box.addItems(["1", "2", "3"])
        self.parallel_box.setCurrentIndex(0)
        self.parallel_box.setMinimumWidth(60)

        self.btn_start = QPushButton("开始刷课")
        self.btn_start.setObjectName("startBtn")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.clicked.connect(self.start)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setObjectName("pauseBtn")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.btn_end = QPushButton("结束")
        self.btn_end.setObjectName("stopBtn")
        self.btn_end.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_end.setEnabled(False)
        self.btn_end.clicked.connect(self.end_now)

        self.btn_status = QPushButton("检查状态")
        self.btn_status.setObjectName("primaryBtn")
        self.btn_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_status.clicked.connect(self.check_status)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.refresh_all)

        self.status_lbl = QLabel(STATUS["idle"][0])
        self.status_lbl.setObjectName("statusLabel")
        self.status_lbl.setProperty("state", "idle")
        self.status_key = "idle"

        self._ctrl_wide = None
        self._relayout_ctrl(True)
        root.addWidget(self.ctrl)

        # 定时刷新
        self.refresh_all()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(2000)

        # 启动 3 秒后后台检查 GitHub 是否有新版本（失败静默）
        QTimer.singleShot(3000, self._start_update_check)

    # ---------- 控制栏自适应 ----------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # resize() 在控件构造完成前就会触发一次，需保护
        if hasattr(self, "ctrl_grid"):
            wide = self.width() >= 940
            self._apply_col_widths(wide)
            self._relayout_ctrl(wide)

    def _apply_col_widths(self, wide):
        """窄窗同步收缩各定宽列，把余量留给类型分布（否则 900 宽下只剩 50 余像素）"""
        if getattr(self, "_cols_wide", None) == wide:
            return
        self._cols_wide = wide
        widths = (156, 52, 46, 142, 76) if wide else (120, 44, 42, 112, 64)
        self.table.setColumnWidth(0, widths[0])
        for col, w in zip((1, 2, 3, 4), widths[1:]):
            self.table.setColumnWidth(col, w)
        # 窄窗列窄，表头换成短词，避免"总目标""类型分布（计分）"被切一半
        heads = COL_HEADERS if wide else COL_HEADERS_NARROW
        self.table.setHorizontalHeaderLabels(heads)

    def _relayout_ctrl(self, wide):
        """宽窗单行、窄窗两行（参数一行、动作+状态一行）。只在跨越阈值时重排"""
        if self._ctrl_wide == wide:
            return
        self._ctrl_wide = wide
        g = self.ctrl_grid
        while g.count():
            g.takeAt(0)
        for c in range(12):
            g.setColumnStretch(c, 0)
        if wide:
            self.ctrl.setMinimumHeight(52)
            g.addWidget(self.lbl_lessons, 0, 0)
            g.addWidget(self.lessons_box, 0, 1)
            g.addWidget(self.lbl_parallel, 0, 2)
            g.addWidget(self.parallel_box, 0, 3)
            g.addWidget(self.btn_start, 0, 4)
            g.addWidget(self.btn_pause, 0, 5)
            g.addWidget(self.btn_end, 0, 6)
            g.addWidget(self.btn_status, 0, 7)
            g.addWidget(self.btn_refresh, 0, 8)
            g.setColumnStretch(9, 1)
            g.addWidget(self.status_lbl, 0, 10)
        else:
            # 窄窗两行：参数一行、动作+状态一行，最小高度必须够两行，否则第二行被裁
            self.ctrl.setMinimumHeight(96)
            g.addWidget(self.lbl_lessons, 0, 0)
            g.addWidget(self.lessons_box, 0, 1)
            g.addWidget(self.lbl_parallel, 0, 2)
            g.addWidget(self.parallel_box, 0, 3)
            # 拉伸只给第 5 列：状态胶囊右对齐不被拉长，"刷新"也不再被撑宽
            g.addWidget(self.status_lbl, 0, 5,
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            g.addWidget(self.btn_start, 1, 0)
            g.addWidget(self.btn_pause, 1, 1)
            g.addWidget(self.btn_end, 1, 2)
            g.addWidget(self.btn_status, 1, 3)
            g.addWidget(self.btn_refresh, 1, 4)
            g.setColumnStretch(4, 0)
            g.setColumnStretch(5, 1)
        self.ctrl.updateGeometry()

    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            proxy = (load_settings().get("proxy") or "").strip()
            self.append_log_note(
                f"设置已保存（代理：{proxy or '仅直连'}），重新检查更新…")
            self._start_update_check()

    def _start_update_check(self):
        def worker():
            try:
                result = fetch_latest_release()
                if result:
                    tag, url = result
                    if _ver_tuple(tag) > _ver_tuple(APP_VERSION):
                        self.log_note.emit(
                            f"发现新版本 v{tag}（当前 v{APP_VERSION}）")
                        self.update_found.emit(tag, url)
                    else:
                        self.log_note.emit(f"已是最新版本 v{tag}")
                else:
                    self.log_note.emit(
                        "检查更新失败：无法连接 GitHub（直连与本地代理均不可用）")
            except Exception as e:
                self.log_note.emit(f"检查更新异常：{str(e)[:100]}")
        self.log_note.emit("正在检查更新…")
        threading.Thread(target=worker, daemon=True).start()

    def _on_update_found(self, tag, url):
        """发现新版：若该版本已被「忽略」则不弹（直到下个版本发布）；否则弹窗三选"""
        if self._is_ignored(tag):
            return
        self._show_update_dialog(tag, url)

    def _is_ignored(self, tag):
        try:
            with open(IGNORE_FILE, "r", encoding="utf-8") as f:
                ignored = f.read().strip()
            return _ver_tuple(ignored) >= _ver_tuple(tag)
        except Exception:
            return False

    def _show_update_dialog(self, tag, url):
        msg = QMessageBox()
        msg.setWindowTitle("发现新版本")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"雨课堂刷课工具有新版本 **v{tag}**\n\n"
            f"当前版本：v{APP_VERSION}"
        )
        dl_btn = msg.addButton("下载更新", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("下次提醒", QMessageBox.ButtonRole.RejectRole)
        ignore_btn = msg.addButton("忽略此版本", QMessageBox.ButtonRole.DestructiveRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == dl_btn:
            self.append_log_note(f"选择下载更新 v{tag}，开始自动下载…")
            self._start_update(tag)
        elif clicked == ignore_btn:
            try:
                with open(IGNORE_FILE, "w", encoding="utf-8") as f:
                    f.write(tag)
            except Exception:
                pass
            self.append_log_note(f"已忽略 v{tag}，下个版本发布后再提醒。")
        else:
            self.append_log_note(f"选择下次提醒 v{tag}。")

    # ---------- 自动更新 ----------
    def _upd_log(self, text):
        try:
            with open(os.path.join(BASE_DIR, "update_dl.log"), "a", encoding="utf-8") as f:
                f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}\n")
        except Exception:
            pass

    def _start_update(self, tag):
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("有刷课任务在运行，跳过自动更新（刷课结束重启程序即可更新）。")
            return
        self._upd_log(f"开始更新 {tag}")
        self._upd_tag = tag
        threading.Thread(target=self._download_update_worker, args=(tag,), daemon=True).start()

    def _download_update_worker(self, tag):
        self._upd_log("下载线程启动")
        try:
            url = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/yuketang-gui.exe"
            upd_dir = os.path.join(BASE_DIR, "update")
            os.makedirs(upd_dir, exist_ok=True)
            dest = os.path.join(upd_dir, "yuketang-gui.exe")
            for proxy in proxy_list():
                try:
                    if proxy:
                        opener = urllib.request.build_opener(
                            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                    else:
                        opener = urllib.request.build_opener()
                    req = urllib.request.Request(url, headers={"User-Agent": "yuketang-auto-update"})
                    with opener.open(req, timeout=30) as r:
                        total = int(r.headers.get("Content-Length", 0) or 0)
                        self._upd_log(f"连接成功，总大小 {total} 字节，走代理={bool(proxy)}")
                        done = 0
                        last_time = time.time()
                        last_done = 0
                        with open(dest, "wb") as f:
                            while True:
                                chunk = r.read(1 << 16)
                                if not chunk:
                                    break
                                f.write(chunk)
                                done += len(chunk)
                                now = time.time()
                                if (total and now - last_time >= 1.0) or done >= total:
                                    pct = int(done * 100 / total) if total else -1
                                    speed = (done - last_done) / max(now - last_time, 0.001) / 1048576
                                    self.upd_progress.emit(pct, done, total, speed)
                                    last_time = now
                                    last_done = done
                    self._upd_log(f"下载完成 {done} 字节")
                    self.upd_finished.emit(dest)
                    return
                except Exception as e:
                    self._upd_log(f"尝试失败({proxy}): {str(e)[:120]}")
                    try:
                        os.remove(dest)
                    except Exception:
                        pass
                    continue
            self.upd_error.emit("网络不可用或下载失败")
        except Exception as e:
            self._upd_log(f"下载异常: {str(e)[:120]}")
            self.upd_error.emit(str(e)[:120])

    def _upd_progress(self, pct, done, total, speed):
        """cmd 风格进度：原地刷新"下载更新"进度行，不刷屏；若最后一行不是进度行则先新起一行，避免顶掉其他日志"""
        if pct >= 0:
            text = (f"下载更新：{pct:3d}% | {done // 1048576}MB / {total // 1048576}MB"
                    f" | {speed:.1f} MB/s")
        else:
            text = f"下载更新：{done // 1048576}MB | {speed:.1f} MB/s"
        try:
            block = self.log_view.document().lastBlock()
            if block.text().startswith("下载更新"):
                cursor = QTextCursor(block)
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                cursor.insertText(text)
            else:
                self.append_log_note(text)
        except Exception:
            self.append_log_note(text)

    def _finish_update(self, new_exe):
        self._upd_log(f"进入安装阶段: {new_exe}")
        try:
            bat = os.path.join(BASE_DIR, "update.bat")
            content = (
                "@echo off\r\n"
                f'set "APP={APP_EXE}"\r\n'
                f'set "NEW={new_exe}"\r\n'
                "set S=%SystemRoot%\\System32\r\n"
                f"%S%\\taskkill /f /im {APP_NAME} >nul 2>&1\r\n"
                "set /a cnt=0\r\n"
                ":wait\r\n"
                "%S%\\ping -n 2 127.0.0.1 >nul 2>&1\r\n"
                "del /f /q \"%APP%\" >nul 2>&1\r\n"
                "if exist \"%APP%\" (\r\n"
                "    set /a cnt+=1\r\n"
                "    if %cnt% geq 15 goto :dodel\r\n"
                "    goto :wait\r\n"
                ")\r\n"
                ":dodel\r\n"
                'move /y "%NEW%" "%APP%" >nul 2>&1\r\n'
                'start "" "%APP%"\r\n'
                'rd /s /q "%~dp0update" >nul 2>&1\r\n'
                'del /f /q "%~f0" >nul 2>&1\r\n'
            )
            with open(bat, "w", encoding="gbk") as f:
                f.write(content)
            subprocess.Popen(
                ["cmd", "/c", bat],
                cwd=BASE_DIR,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                ),
            )
            self.append_log_note("下载完成，程序将自动重启完成安装…")
            QApplication.instance().quit()
        except Exception as e:
            self._upd_log(f"安装更新失败: {str(e)[:120]}")
            self.append_log_note(f"安装更新失败: {e}")

    def _upd_failed(self, err=""):
        self._upd_log(f"更新失败: {err}")
        self.append_log_note(
            f"自动更新失败：{err}（可前往 GitHub Releases 手动下载）")

    # ---------- 状态机（第 4 层：文案与颜色单一来源）----------
    def _set_status(self, key):
        if key not in STATUS:
            key = "idle"
        if self.status_key == key:
            return
        self.status_key = key
        self.status_lbl.setText(STATUS[key][0])
        self.status_lbl.setProperty("state", key)
        st = self.status_lbl.style()
        st.unpolish(self.status_lbl)
        st.polish(self.status_lbl)

    # ---------- 进度看板（第 5 层：diff 更新 + 控件复用）----------
    def _load_progress(self):
        """返回 (data, 错误文案)。区分 缺失 / 损坏 / 其他 IO 失败"""
        if not os.path.exists(PROGRESS_FILE):
            return None, "尚未生成 progress.json"
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return None, "progress.json 内容损坏，无法解析"
        except Exception as e:
            return None, f"progress.json 读取失败：{str(e)[:60]}"
        if not isinstance(data, dict) or not isinstance(data.get("courses"), dict):
            return None, "progress.json 结构异常"
        return data, ""

    def _show_empty(self, reason):
        self.empty_sub.setText(f"{reason}。\n点「检查状态」扫描课程后，这里会显示每门课的进度。")
        self.table_stack.setCurrentIndex(1)
        self.updated_lbl.setText("更新：—")
        # 彻底清空旧行与缓存，避免切回表格时闪旧数据（已空则跳过，避免每轮无谓重绘）
        if self.table.rowCount() or self._order:
            self.table.clearContents()
            self.table.setRowCount(0)
            self._order = []
            self._rows = {}
            self._holders = {}
            self._cache = {}

    def _load_running(self):
        try:
            with open(os.path.join(BASE_DIR, "tasks.json"), "r", encoding="utf-8") as f:
                tdata = json.load(f)
            return {t["name"] for t in tdata.get("tasks", []) if t.get("status") == "running"}
        except Exception:
            return set()

    def refresh_progress(self):
        prog, err = self._load_progress()
        if err:
            self._show_empty(err)
            return
        courses = prog.get("courses") or {}
        if not courses:
            self._show_empty("progress.json 里还没有任何课程记录")
            return
        self.table_stack.setCurrentIndex(0)
        self.updated_lbl.setText("更新：" + (prog.get("updated") or "—"))

        running_names = self._load_running()
        is_running = self.proc is not None and self.proc.poll() is None
        names = list(courses.keys())

        # 仅当课程集合/顺序变化时才重建行结构（不是每轮）
        if names != self._order:
            self._rebuild_rows(names)

        for row, name in enumerate(names):
            self._update_row(row, name, courses[name], running_names, is_running)

    def _rebuild_rows(self, names):
        self.table.clearContents()
        self.table.setRowCount(len(names))
        self._order = list(names)
        self._rows = {n: i for i, n in enumerate(names)}
        self._holders = {}
        self._cache = {}
        for row, name in enumerate(names):
            holder = QWidget()
            lay = QVBoxLayout(holder)
            lay.setContentsMargins(8, 3, 8, 3)
            lay.setSpacing(2)
            bar = RoundBar()
            bar.setObjectName("courseBar")
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.setFixedHeight(16)
            lay.addWidget(bar)
            # 第二行：分类型完成度（视频 2/21 · 课件 0/8 …），宽度不够走省略号，全文在 ToolTip
            sub = QLabel("—")
            sub.setObjectName("barSub")
            sub.setFixedHeight(13)
            lay.addWidget(sub)
            self._holders[name] = holder
            self.table.setCellWidget(row, 3, holder)

    def _update_row(self, row, name, info, running_names, is_running):
        total = int(info.get("total", 0) or 0)
        remain = int(info.get("remaining", 0) or 0)
        done = bool(info.get("done"))
        done_n = max(0, total - remain) if total else 0

        # 状态（唯一来源：COURSE_STATUS）
        if total == 0 or int(info.get("target", 0) or 0) == 0:
            skey = "none"
        elif done:
            skey = "done"
        elif is_running and name in running_names:
            skey = "checking" if self.running_mode == "检查" else "running"
        else:
            skey = "waiting"
        status_text, status_fg, status_bg = COURSE_STATUS[skey]

        td = info.get("type_dist") or {}
        td_items = sorted_types(td)
        td_text = " ".join(f"{k}×{v}" for k, v in td_items) if td_items else "—"
        ratio = (total - remain) / total if total else 0.0
        pct = int(round(ratio * 100))
        full = done or (total > 0 and remain == 0)

        # 分类型完成度：基线（各类型总数）减去当前剩余 = 该类已刷多少
        tb = info.get("type_base") or {}
        sub_text = ""
        if td_items:
            parts = []
            for k, v in td_items:
                base_n = max(int(tb.get(k, 0) or 0), int(v))
                parts.append(f"{k} {base_n - int(v)}/{base_n}")
            sub_text = " · ".join(parts)
        elif tb:
            parts = [f"{k} {int(base_n)}/{int(base_n)}" for k, base_n in sorted_types(tb)]
            sub_text = " · ".join(parts)

        key = (total, remain, done, skey, td_text, sub_text)
        if self._cache.get(name) == key:
            return                       # 该行无变化，一个 cell 都不碰
        self._cache[name] = key

        it0 = self.table.item(row, 0)
        if it0 is None:
            it0 = QTableWidgetItem(name)
            it0.setToolTip(name)
            self.table.setItem(row, 0, it0)
        elif it0.text() != name:
            it0.setText(name)
            it0.setToolTip(name)

        for col, text, fg in (
            (1, str(total), T_TEXT_1),
            (2, str(remain), T_TEXT_4 if total == 0 else (T_WARNING if remain else T_SUCCESS)),
        ):
            it = self.table.item(row, col)
            if it is None:
                it = QTableWidgetItem(text)
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                it.setForeground(QColor(fg))
                self.table.setItem(row, col, it)
            else:
                if it.text() != text:
                    it.setText(text)
                if it.foreground().color().name().lower() != QColor(fg).name().lower():
                    it.setForeground(QColor(fg))

        st = self.table.item(row, 4)
        if st is None:
            st = QTableWidgetItem(status_text)
            st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 4, st)
        elif st.text() != status_text:
            st.setText(status_text)
        st.setForeground(QColor(status_fg))
        st.setBackground(QColor(*status_bg))

        itd = self.table.item(row, 5)
        if itd is None:
            itd = QTableWidgetItem(td_text)
            itd.setForeground(QColor(T_TEXT_3))
            itd.setToolTip(td_text)
            self.table.setItem(row, 5, itd)
        elif itd.text() != td_text:
            itd.setText(td_text)
            itd.setToolTip(td_text)

        holder = self._holders.get(name)
        if holder is not None:
            bar = holder.findChild(QProgressBar)
            if bar is not None:
                if bar.value() != pct:
                    bar.setValue(pct)
                fmt = f"{done_n} / {total}"
                if bar.format() != fmt:
                    bar.setFormat(fmt)
                if bar.property("full") != full:
                    bar.setProperty("full", full)
                    s = bar.style()
                    s.unpolish(bar)
                    s.polish(bar)

            sub = holder.findChild(QLabel)
            if sub is not None:
                shown = sub.fontMetrics().elidedText(
                    sub_text or "—", Qt.TextElideMode.ElideRight, sub.width() - 2)
                if sub.text() != shown:
                    sub.setText(shown)
                tip = sub_text or "—"
                if sub.toolTip() != tip:
                    sub.setToolTip(tip)

    # ---------- 日志（第 6 层：增量追加 + 智能跟随）----------
    def _log_at_bottom(self):
        sb = self.log_view.verticalScrollBar()
        return sb.value() >= sb.maximum() - 4

    def refresh_log(self):
        try:
            size = os.path.getsize(LOG_FILE)
        except OSError:
            return
        if not self._log_inited:
            self._log_offset = max(0, size - 40000)   # 首次只显示尾部 40KB
            self._log_inited = True
        if size < self._log_offset:                    # 文件被截断/清空
            self._log_offset = 0
            self.log_view.clear()
        if size == self._log_offset:
            return
        at_bottom = self._log_at_bottom()
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(self._log_offset)
                raw = f.read(1 << 16)
        except OSError:
            return
        if not raw:
            return
        nl = raw.rfind(b"\n")
        if nl < 0:
            return                                     # 还没写完一整行，等下一轮
        self._log_offset += nl + 1
        text = raw[:nl + 1].decode("utf-8", errors="replace").rstrip("\n")
        if self.log_view.toPlainText():
            self.log_view.appendPlainText(text)
        else:
            self.log_view.setPlainText(text)
        if at_bottom:
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def refresh_all(self):
        self.refresh_progress()
        self.refresh_log()
        self._sync_state()

    def _sync_state(self):
        p = self.proc
        if p is None:
            return
        rc = p.poll()
        if rc is None:
            if self.status_key == "paused":
                return
            self._set_status("checking" if self.running_mode == "检查" else "running")
            return
        self.proc = None
        if self._manual_stop:
            self._manual_stop = False
            self._set_status("stopped")
        elif rc == 0:
            self._set_status("finished")
        else:
            self._set_status("error")
        self.btn_pause.setText("暂停")
        self.btn_pause.setEnabled(False)
        self.btn_end.setEnabled(False)

    # ---------- 控制 ----------
    def launch_runner(self, extra_args, mode_label):
        """启动刷课/检查逻辑：源码环境 subprocess 调 venv python；打包后 subprocess 调自身（--auto-cli）。
        刷课逻辑运行在独立进程，与 GUI 完全隔离。"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“结束”。")
            return False
        cmd = auto_cmd(extra_args)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=BASE_DIR,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.append_log_note(f"启动失败: {e}")
            return False
        self._manual_stop = False
        self.append_log_note(f"已启动：{' '.join(extra_args)}（{mode_label}）")
        self._clear_pause()
        self.btn_pause.setText("暂停")
        self.btn_pause.setEnabled(True)
        self.btn_end.setEnabled(True)
        return True

    def start(self):
        """刷课模式：完整播放每个视频"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“结束”。")
            return
        if not FROZEN and not os.path.exists(AUTO_PY):
            self.append_log_note(f"找不到脚本：{AUTO_PY}")
            return
        # 首次运行（还没有任何进度记录）：建议先检查状态
        if not self._has_progress():
            msg = QMessageBox()
            msg.setWindowTitle("首次运行")
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setText(
                "首次运行前建议先「检查状态」\n\n"
                "检查会刷新课程与未完成数量，避免误判“已刷完”。"
            )
            btn_check = msg.addButton("去检查状态", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("直接刷课", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            if msg.clickedButton() == btn_check:
                self.check_status()
                return
        n = self.lessons_box.currentText()
        pn = self.parallel_box.currentText()
        extra = []
        if n != "全部":
            extra += ["--max-lessons", n]
        if pn != "1":
            extra += ["--parallel", pn]
        if not self.launch_runner(extra, f"刷课模式，每门 {n} 条，并行 {pn} 窗口"):
            return
        self.running_mode = "刷课"
        self._set_status("running")

    def _has_progress(self):
        """是否已有进度记录（跑过检查/刷课）"""
        try:
            return os.path.exists(PROGRESS_FILE) and os.path.getsize(PROGRESS_FILE) > 0
        except Exception:
            return False

    def check_status(self):
        """检查模式：快速遍历所有课程，解析目标并刷新进度记录（不播放）"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“结束”。")
            return
        if not FROZEN and not os.path.exists(AUTO_PY):
            self.append_log_note(f"找不到脚本：{AUTO_PY}")
            return
        if not self.launch_runner(["--quick"], "检查模式：遍历刷新进度，不播放"):
            return
        self.running_mode = "检查"
        self._set_status("checking")

    def toggle_pause(self):
        """暂停/继续：写或删 pause.flag，刷课进程检测后暂停/恢复播放"""
        if os.path.exists(PAUSE_FILE):
            try:
                os.remove(PAUSE_FILE)
            except Exception:
                pass
            self.btn_pause.setText("暂停")
            if self.proc is not None and self.proc.poll() is None:
                self._set_status("checking" if self.running_mode == "检查" else "running")
            self.append_log_note("已发送「继续」指令，刷课恢复。")
        else:
            try:
                with open(PAUSE_FILE, "w", encoding="utf-8") as f:
                    f.write("1")
            except Exception as e:
                self.append_log_note(f"发送暂停指令失败: {e}")
                return
            self.btn_pause.setText("继续")
            if self.proc is not None and self.proc.poll() is None:
                self._set_status("paused")
            self.append_log_note("已发送「暂停」指令，当前条目暂停播放，进度不计入。")

    def end_now(self):
        """结束：彻底关闭刷课脚本 + 所有刷课用的 Edge 浏览器 + 残留进程"""
        self._clear_pause()
        self._manual_stop = True
        self.stop()
        self.btn_pause.setText("暂停")
        self.btn_pause.setEnabled(False)
        self.btn_end.setEnabled(False)
        self.append_log_note("已结束：刷课脚本与浏览器均已关闭。")

    def _clear_pause(self):
        try:
            if os.path.exists(PAUSE_FILE):
                os.remove(PAUSE_FILE)
        except Exception:
            pass

    def _run_cleanup(self):
        """进程树清理（psutil 原生，不依赖 PowerShell）：
        1) auto-cli / multiprocessing-fork 的刷课脚本进程
        2) 刷课 Edge（命令行含 edge_profile）
        3) python + yuketang 残留"""
        try:
            import psutil
        except Exception:
            self._run_cleanup_ps()
            return
        killed = []
        try:
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = (proc.info["name"] or "").lower()
                    cmd = " ".join(proc.info["cmdline"] or [])
                except Exception:
                    continue
                hit = False
                if "yuketang" in name and ("--auto-cli" in cmd or "--multiprocessing-fork" in cmd):
                    hit = True
                elif name == "msedge.exe" and "edge_profile" in cmd:
                    hit = True
                elif name.startswith("python") and "yuketang" in cmd:
                    hit = True
                if hit:
                    try:
                        proc.kill()
                        killed.append(proc.pid)
                    except Exception:
                        pass
        except Exception:
            pass
        self.append_log_note(f"已清理残留进程：{len(killed)} 个")

    def _run_cleanup_ps(self):
        """psutil 不可用时的 PowerShell 兜底清理"""
        ps = r'''
$cli = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'yuketang' -and ($_.CommandLine -match '--auto-cli' -or $_.CommandLine -match '--multiprocessing-fork') })
foreach ($p in $cli) { & taskkill /f /t /pid $p.ProcessId 2>$null | Out-Null }
$ms = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'msedge' -and $_.CommandLine -match 'edge_profile' })
foreach ($p in $ms) { & taskkill /f /t /pid $p.ProcessId 2>$null | Out-Null }
'''
        ps_file = os.path.join(BASE_DIR, "_cleanup.ps1")
        try:
            with open(ps_file, "w", encoding="utf-8-sig") as f:
                f.write(ps)
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_file],
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        finally:
            try:
                os.remove(ps_file)
            except Exception:
                pass

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None
        self._run_cleanup()
        self._set_status("stopped")
        self.append_log_note("已手动停止，并清理了残留进程。")

    def append_log_note(self, text):
        at_bottom = self._log_at_bottom()
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}"
        if self.log_view.toPlainText():
            self.log_view.appendPlainText(line)
        else:
            self.log_view.setPlainText(line)
        if at_bottom:
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())


def _ver_tuple(tag):
    """'v1.0.0' / '1.0.1' → (1, 0, 0)"""
    out = []
    for p in tag.lstrip("vV").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


class SettingsDialog(QDialog):
    """设置窗口：可修改检查更新/下载使用的代理地址"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(440, 200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        lbl = QLabel("检查更新 / 下载使用的代理地址：")
        lbl.setObjectName("fieldLabel")
        self.proxy_edit = QLineEdit(load_settings().get("proxy") or DEFAULT_PROXY)
        self.proxy_edit.setPlaceholderText("例如 http://127.0.0.1:12450")
        tip = QLabel("留空并保存 = 仅直连 GitHub（不使用代理）")
        tip.setObjectName("metaLabel")
        btn_row = QHBoxLayout()
        check_btn = QPushButton("检查更新")
        check_btn.setToolTip("立即用当前代理设置检查 GitHub 新版本（保存代理后生效）")
        check_btn.clicked.connect(self._check_now)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("ghostBtn")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(check_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        lay.addWidget(lbl)
        lay.addWidget(self.proxy_edit)
        lay.addWidget(tip)
        lay.addStretch(1)
        lay.addLayout(btn_row)

    def _check_now(self):
        """手动检查更新：先按输入框里的代理保存，再触发主窗口重新检查"""
        save_settings({"proxy": self.proxy_edit.text().strip()})
        parent = self.parent()
        if parent is not None and hasattr(parent, "_start_update_check"):
            parent._start_update_check()
        self.accept()

    def _save(self):
        save_settings({"proxy": self.proxy_edit.text().strip()})
        self.accept()


def load_settings():
    """读取本地设置（settings.json），无文件/异常返回空字典"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def proxy_list():
    """检查更新/下载走代理的顺序：直连 → 设置里配置的代理；
    没配置过代理时默认用 DEFAULT_PROXY（保持旧行为）。"""
    cfg = (load_settings().get("proxy") or "").strip()
    if cfg:
        return [None, cfg]
    return [None, DEFAULT_PROXY]


def _make_opener(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _fetch_via_html(proxy):
    """通道 1：HTML 302 跳转（github.com/.../releases/latest → /releases/tag/vX.Y.Z）。
    不走 API，没有匿名限流。"""
    url = f"https://github.com/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "yuketang-auto-check"})
    with _make_opener(proxy).open(req, timeout=6) as r:
        final = r.geturl()
        tag = final.rstrip("/").rsplit("/", 1)[-1]
        if tag and tag.lower().startswith("v"):
            return tag, final
    return None


def _fetch_via_api(proxy):
    """通道 2：GitHub API（未限流时最权威；匿名限流 60 次/小时可能 403，作为兜底）"""
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(api, headers={"User-Agent": "yuketang-auto-check"})
    with _make_opener(proxy).open(req, timeout=6) as r:
        data = json.loads(r.read().decode("utf-8"))
        tag = data.get("tag_name", "")
        if tag:
            return tag, data.get("html_url", GITHUB_RELEASE_URL)
    return None


def fetch_latest_release():
    """后台查询 GitHub 最新 Release，双通道：API 优先（未限流时最快最权威），HTML 兜底（防限流 403）。
    每个通道都按 直连 → 配置代理 的顺序尝试。成功返回 (tag, release_url)，全部失败返回 None"""
    for fetch_fn in (_fetch_via_api, _fetch_via_html):
        for proxy in proxy_list():
            try:
                r = fetch_fn(proxy)
                if r:
                    return r
            except Exception:
                continue
    return None


def _apply_palette(app):
    """显式固定深色 QPalette，不跟随系统主题（防止 Windows 深色/浅色模式串色）"""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(T_BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(T_TEXT_1))
    pal.setColor(QPalette.ColorRole.Base, QColor(T_SURFACE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(T_SURFACE_ALT))
    pal.setColor(QPalette.ColorRole.Text, QColor(T_TEXT_1))
    pal.setColor(QPalette.ColorRole.Button, QColor(T_SURFACE_ALT))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(T_TEXT_1))
    pal.setColor(QPalette.ColorRole.BrightText, QColor(T_DANGER_HI))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(T_ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(T_SURFACE_ALT))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(T_TEXT_1))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(T_TEXT_4))
    pal.setColor(QPalette.ColorRole.Light, QColor(T_BORDER))
    pal.setColor(QPalette.ColorRole.Midlight, QColor(T_SURFACE_ALT))
    pal.setColor(QPalette.ColorRole.Mid, QColor(T_BORDER))
    pal.setColor(QPalette.ColorRole.Dark, QColor(T_SUNKEN))
    pal.setColor(QPalette.ColorRole.Shadow, QColor(T_SUNKEN))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.ToolTipText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(T_DIS_TEXT))
    app.setPalette(pal)


def main():
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv)
    _apply_palette(app)
    app.setStyleSheet(QSS)
    win = Console()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    # 打包后 multiprocessing spawn 子进程需要此行（auto 内部并行用）
    multiprocessing.freeze_support()
    # 单文件双模式：GUI 用 subprocess 调自身带 --auto-cli 参数时，直接跑刷课逻辑、不打开窗口
    if FROZEN and CLI_FLAG in sys.argv:
        args = [a for a in sys.argv[1:] if a != CLI_FLAG]
        try:
            auto.main(args)
        except Exception:
            import traceback
            try:
                with open(os.path.join(BASE_DIR, "auto_err.txt"), "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except Exception:
                pass
            sys.exit(1)
        sys.exit(0)
    main()
