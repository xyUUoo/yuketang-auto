# -*- coding: utf-8 -*-
"""
雨课堂刷课控制台（PySide6 / Qt 专业版）
================================================
- 进度看板：表格展示各课程 总目标/剩余/完成度进度条/状态
- 实时日志：只读日志区，自动滚动显示 log.txt 最新内容
- 一键控制：开始刷课（完整刷/快速检测 可选）/ 停止（含清理残留 Edge）/ 只看状态 / 刷新
- Qt 深色专业样式

运行：双击 gui.bat 或 python gui.py
"""
import datetime
import json
import multiprocessing
import os
import subprocess
import sys

import auto  # 内嵌核心刷课逻辑（打包后随本程序一起分发）

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_PY = os.path.join(BASE_DIR, "auto.py")
PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")

# 打包（PyInstaller）后：不再调用外部 exe，直接用 multiprocessing 拉起内嵌的 auto 逻辑
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BASE_DIR = os.path.dirname(sys.executable)
    PYTHON = None
    PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
    LOG_FILE = os.path.join(BASE_DIR, "log.txt")
else:
    PYTHON = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")


def auto_runner(argv):
    """子进程入口：执行内嵌刷课逻辑（spawn 目标，须为模块级函数）"""
    auto.main(argv)


def auto_cmd(extra_args):
    """构造调用 auto 的参数：源码环境用 venv python，打包后 spawn 内嵌逻辑"""
    if FROZEN:
        return extra_args
    return [PYTHON, AUTO_PY] + extra_args

QSS = """
QMainWindow, QWidget { background-color: #1e1f26; color: #e6e6e6; }
QLabel { color: #e6e6e6; }
QTableWidget {
    background-color: #262833; alternate-background-color: #2b2d3a;
    border: 1px solid #3a3d4d; border-radius: 8px;
    gridline-color: #333647; font-size: 13px;
}
QTableWidget::item { padding: 6px 4px; }
QHeaderView::section {
    background-color: #2f3242; color: #c9cdd8; border: none;
    border-bottom: 2px solid #3f4456; padding: 8px 4px; font-size: 13px; font-weight: bold;
}
QPlainTextEdit {
    background-color: #14151b; color: #b8c7dc; border: 1px solid #3a3d4d;
    border-radius: 8px; font-family: Consolas; font-size: 12px; padding: 6px;
}
QPushButton {
    background-color: #33364a; color: #e6e6e6; border: none;
    border-radius: 6px; padding: 8px 16px; font-size: 13px; font-weight: 500;
}
QPushButton:hover { background-color: #40445c; }
QPushButton:pressed { background-color: #2a2c3a; }
QPushButton#startBtn { background-color: #2ea043; color: white; }
QPushButton#startBtn:hover { background-color: #34b44c; }
QPushButton#stopBtn { background-color: #c0392b; color: white; }
QPushButton#stopBtn:hover { background-color: #d44535; }
QPushButton#primaryBtn { background-color: #5b6ee1; color: white; }
QPushButton#primaryBtn:hover { background-color: #6c7fe8; }
QComboBox {
    background-color: #2b2d3a; color: #e6e6e6; border: 1px solid #3a3d4d;
    border-radius: 6px; padding: 6px 10px;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #262833; color: #e6e6e6; border: 1px solid #3a3d4d;
    selection-background-color: #5b6ee1; selection-color: white;
}
QProgressBar {
    background-color: #33364a; border: none; border-radius: 5px;
    text-align: center; color: white; font-size: 11px; min-height: 12px;
}
QProgressBar::chunk { border-radius: 5px; background-color: #5b6ee1; }
QSplitter::handle { background-color: #33364a; width: 4px; }
QScrollBar:vertical { background: #262833; width: 10px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #4a4f68; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


class Console(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("雨课堂刷课控制台")
        self.resize(980, 700)
        self.setMinimumSize(800, 560)

        self.proc = None
        self.running_mode = ""
        self.last_log_size = 0

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ===== 标题行 =====
        head = QHBoxLayout()
        title = QLabel("雨课堂刷课控制台")
        title.setFont(QFont("Microsoft YaHei UI", 17, QFont.Weight.Bold))
        head.addWidget(title)
        head.addStretch(1)
        self.updated_lbl = QLabel("更新：—")
        self.updated_lbl.setStyleSheet("color: #9aa0b4; font-size: 12px;")
        head.addWidget(self.updated_lbl)
        root.addLayout(head)

        # ===== 中间分栏：表格 + 日志 =====
        split = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["课程", "总目标", "剩余", "完成度", "状态"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 4):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, 180)
        split.addWidget(self.table)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        root.addWidget(split, 1)

        # ===== 控制栏 =====
        ctrl = QHBoxLayout()
        ctrl.addWidget(self._mk_label("每门条数："))
        self.lessons_box = QComboBox()
        self.lessons_box.addItems(["全部", "1", "2", "3", "5", "10"])
        self.lessons_box.setCurrentIndex(0)
        self.lessons_box.setMinimumWidth(70)
        ctrl.addWidget(self.lessons_box)

        ctrl.addSpacing(12)
        ctrl.addWidget(self._mk_label("并行数："))
        self.parallel_box = QComboBox()
        self.parallel_box.addItems(["1", "2", "3"])
        self.parallel_box.setCurrentIndex(0)
        self.parallel_box.setMinimumWidth(60)
        ctrl.addWidget(self.parallel_box)

        ctrl.addSpacing(16)
        self.btn_start = QPushButton("开始刷课")
        self.btn_start.setObjectName("startBtn")
        self.btn_start.clicked.connect(self.start)
        ctrl.addWidget(self.btn_start)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.clicked.connect(self.stop)
        ctrl.addWidget(self.btn_stop)

        self.btn_status = QPushButton("检查状态")
        self.btn_status.setObjectName("primaryBtn")
        self.btn_status.clicked.connect(self.check_status)
        ctrl.addWidget(self.btn_status)

        self.btn_refresh = QPushButton("↻ 刷新")
        self.btn_refresh.clicked.connect(self.refresh_all)
        ctrl.addWidget(self.btn_refresh)

        ctrl.addStretch(1)
        self.status_lbl = QLabel("空闲")
        self.status_lbl.setStyleSheet("color: #2ea043; font-size: 14px; font-weight: bold;")
        ctrl.addWidget(self.status_lbl)
        root.addLayout(ctrl)

        # 定时刷新
        self.refresh_all()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(2000)

    def _mk_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #9aa0b4; font-size: 13px;")
        return lbl

    # ---------- 进度看板 ----------
    def refresh_progress(self):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                prog = json.load(f)
        except Exception:
            self.updated_lbl.setText("更新：—（尚未生成 progress.json）")
            return
        courses = prog.get("courses", {})
        self.updated_lbl.setText("更新：" + prog.get("updated", "—"))

        # 读取并行任务池：哪些课程正在被某个窗口刷（running）
        running_names = set()
        try:
            with open(os.path.join(BASE_DIR, "tasks.json"), "r", encoding="utf-8") as f:
                tdata = json.load(f)
            running_names = {t["name"] for t in tdata.get("tasks", []) if t["status"] == "running"}
        except Exception:
            pass
        is_running = self.proc is not None and self.proc.poll() is None

        self.table.setRowCount(len(courses))
        for row, (name, info) in enumerate(courses.items()):
            total = info.get("total", 0)
            remain = info.get("remaining", 0)
            done = bool(info.get("done"))
            done_n = max(0, total - remain) if total else 0

            item_name = QTableWidgetItem(name)
            item_name.setForeground(QColor("#e6e6e6"))
            self.table.setItem(row, 0, item_name)

            item_total = QTableWidgetItem(str(total))
            item_total.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 1, item_total)

            item_rem = QTableWidgetItem(str(remain))
            item_rem.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_rem.setForeground(QColor("#d29922" if remain else "#2ea043"))
            self.table.setItem(row, 2, item_rem)

            bar = QProgressBar()
            ratio = (total - remain) / total if total else 0.0
            bar.setRange(0, 100)
            bar.setValue(int(round(ratio * 100)))
            bar.setFormat(f"{done_n} / {total}")
            bar.setStyleSheet(
                "QProgressBar { background-color: #33364a; border: none; border-radius: 5px;"
                " text-align: center; color: white; font-size: 11px; min-height: 14px; }"
                "QProgressBar::chunk { border-radius: 5px; background-color: %s; }"
                % ("#2ea043" if done else "#5b6ee1")
            )
            self.table.setCellWidget(row, 3, bar)

            # 状态：已刷完 / 无任务 / 正在刷（仅运行中）/ 等待中
            status, st_color = "等待中", "#5b6ee1"
            if info.get("target", 0) == 0:
                status, st_color = "无任务", "#8a91a8"
            elif done:
                status, st_color = "已刷完 ✓", "#2ea043"
            elif is_running and name in running_names:
                status, st_color = "刷课中", "#d29922"
            item_st = QTableWidgetItem(status)
            item_st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_st.setForeground(QColor(st_color))
            self.table.setItem(row, 4, item_st)

    # ---------- 日志 ----------
    def refresh_log(self):
        try:
            size = os.path.getsize(LOG_FILE)
        except Exception:
            return
        if size == self.last_log_size:
            return
        self.last_log_size = size
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                f.seek(max(0, size - 40000))
                content = f.read()
        except Exception:
            return
        self.log_view.setPlainText(content)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def refresh_all(self):
        self.refresh_progress()
        self.refresh_log()
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            self.status_lbl.setText("空闲（上次运行已结束）")
            self.status_lbl.setStyleSheet("color: #2ea043; font-size: 14px; font-weight: bold;")
        elif self.proc is not None:
            mode_txt = "检查中…" if getattr(self, "running_mode", "") == "检查" else "刷课中…"
            self.status_lbl.setText(mode_txt)
            self.status_lbl.setStyleSheet("color: #d29922; font-size: 14px; font-weight: bold;")

    # ---------- 控制 ----------
    def launch_runner(self, extra_args, mode_label):
        """启动刷课/检查逻辑：源码环境 subprocess 调 venv python；打包后 multiprocessing spawn 内嵌逻辑"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“停止”。")
            return False
        args = auto_cmd(extra_args)
        if FROZEN:
            try:
                mp_ctx = multiprocessing.get_context("spawn")
                self.proc = mp_ctx.Process(target=auto_runner, args=(args,))
                self.proc.start()
            except Exception as e:
                self.append_log_note(f"启动失败: {e}")
                return False
        else:
            try:
                self.proc = subprocess.Popen(
                    [PYTHON, AUTO_PY] + extra_args, cwd=BASE_DIR,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as e:
                self.append_log_note(f"启动失败: {e}")
                return False
        self.append_log_note(f"已启动：{' '.join(extra_args)}（{mode_label}）")
        return True

    def start(self):
        """刷课模式：完整播放每个视频"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“停止”。")
            return
        if not FROZEN and not os.path.exists(AUTO_PY):
            self.append_log_note(f"找不到脚本：{AUTO_PY}")
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
        self.status_lbl.setText("刷课中…")
        self.status_lbl.setStyleSheet("color: #d29922; font-size: 14px; font-weight: bold;")

    def check_status(self):
        """检查模式：快速遍历所有课程，解析目标并刷新进度记录（不播放）"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“停止”。")
            return
        if not FROZEN and not os.path.exists(AUTO_PY):
            self.append_log_note(f"找不到脚本：{AUTO_PY}")
            return
        if not self.launch_runner(["--quick"], "检查模式：遍历刷新进度，不播放"):
            return
        self.running_mode = "检查"
        self.status_lbl.setText("检查中…")
        self.status_lbl.setStyleSheet("color: #5b6ee1; font-size: 14px; font-weight: bold;")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        ps = r"""
$ps = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match 'yuketang') -or
    ($_.Name -match 'msedge' -and $_.CommandLine -match 'edge_profile') -or
    ($_.Name -match 'python' -and $_.CommandLine -match 'yuketang')
}
foreach ($p in $ps) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
"""
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
        self.status_lbl.setText("已停止")
        self.status_lbl.setStyleSheet("color: #2ea043; font-size: 14px; font-weight: bold;")
        self.append_log_note("已手动停止，并清理了残留进程。")

    def append_log_note(self, text):
        self.log_view.appendPlainText(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}")
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())


def main():
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    win = Console()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    # PyInstaller 打包后 multiprocessing spawn 子进程需要此行，否则子进程会重复打开窗口
    multiprocessing.freeze_support()
    main()
