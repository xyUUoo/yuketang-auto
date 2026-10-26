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
import multiprocessing  # 仅用于 freeze_support（auto 内部并行 spawn）
import os
import subprocess
import sys
import threading
import urllib.request
import webbrowser

import auto  # 内嵌核心刷课逻辑（打包后随本程序一起分发）

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

# ---- 版本与更新检查 ----
APP_VERSION = "1.0.2"        # 每次发新版前手动递增
GITHUB_REPO = "xyUUoo/yuketang-auto"
GITHUB_RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_PY = os.path.join(BASE_DIR, "auto.py")
PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")

# 打包（PyInstaller）后：单文件双模式 —— 双击=GUI；GUI 内部用 subprocess 调自身
# （--auto-cli 参数）跑刷课逻辑，与 GUI 完全隔离，互不干扰
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BASE_DIR = os.path.dirname(sys.executable)
    PYTHON = None
    PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
    LOG_FILE = os.path.join(BASE_DIR, "log.txt")
else:
    PYTHON = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")

# CLI 模式标志：打包后 subprocess 调用自身时附加，入口据此直接跑刷课逻辑
CLI_FLAG = "--auto-cli"


def auto_cmd(extra_args):
    """构造调用 auto 的命令：源码环境用 venv python；打包后调自身（--auto-cli）"""
    if FROZEN:
        return [sys.executable, CLI_FLAG] + extra_args
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
    # 跨线程信号：后台检查线程发现新版本 → 主线程弹窗
    update_found = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("雨课堂刷课控制台")
        self.resize(980, 700)
        self.setMinimumSize(800, 560)

        self.proc = None
        self.running_mode = ""
        self.last_log_size = 0
        self.update_found.connect(self._show_update_dialog)

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

        # 启动 3 秒后后台检查 GitHub 是否有新版本（失败静默）
        QTimer.singleShot(3000, self._start_update_check)

    def _start_update_check(self):
        def worker():
            try:
                result = fetch_latest_release()
                if result:
                    tag, url = result
                    if _ver_tuple(tag) > _ver_tuple(APP_VERSION):
                        self.update_found.emit(tag, url)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _show_update_dialog(self, tag, url):
        msg = QMessageBox()
        msg.setWindowTitle("发现新版本")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"雨课堂刷课工具有新版本 **{tag}**\n\n"
            f"当前版本：v{APP_VERSION}\n\n"
            f"点击「下载更新」会自动下载并重启为新版，全程无需手动操作。"
        )
        ok_btn = msg.addButton("下载更新", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("忽略", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        if msg.clickedButton() == ok_btn:
            self._start_update(tag)

    # ---------- 自动更新 ----------
    def _start_update(self, tag):
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("有刷课任务在运行，请先点“停止”后再更新。")
            return
        self._upd_tag = tag
        self._upd_dlg = QDialog(self)
        self._upd_dlg.setWindowTitle("正在更新")
        self._upd_dlg.setFixedSize(440, 130)
        lay = QVBoxLayout(self._upd_dlg)
        self._upd_lbl = QLabel(f"正在下载新版本 {tag} …")
        self._upd_lbl.setStyleSheet("color: #e6e6e6; font-size: 13px;")
        self._upd_bar = QProgressBar()
        self._upd_bar.setRange(0, 100)
        self._upd_bar.setValue(0)
        self._upd_bar.setFormat("%p%")
        lay.addWidget(self._upd_lbl)
        lay.addWidget(self._upd_bar)
        self._upd_dlg.show()
        threading.Thread(target=self._download_update_worker, args=(tag,), daemon=True).start()

    def _download_update_worker(self, tag):
        try:
            url = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/yuketang-gui.exe"
            upd_dir = os.path.join(BASE_DIR, "update")
            os.makedirs(upd_dir, exist_ok=True)
            dest = os.path.join(upd_dir, "yuketang-gui.exe")
            last_pct = -1
            last_chunk = 0
            for proxy in (None, "http://127.0.0.1:12450"):
                try:
                    if proxy:
                        opener = urllib.request.build_opener(
                            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                    else:
                        opener = urllib.request.build_opener()
                    req = urllib.request.Request(url, headers={"User-Agent": "yuketang-auto-update"})
                    with opener.open(req, timeout=30) as r:
                        total = int(r.headers.get("Content-Length", 0) or 0)
                        done = 0
                        with open(dest, "wb") as f:
                            while True:
                                chunk = r.read(1 << 16)
                                if not chunk:
                                    break
                                f.write(chunk)
                                done += len(chunk)
                                if total:
                                    pct = int(done * 100 / total)
                                    if pct != last_pct:
                                        last_pct = pct
                                        QTimer.singleShot(0, lambda p=pct, d=done, t=total:
                                                          self._upd_progress(p, d, t))
                                elif done - last_chunk >= 512 * 1024:
                                    last_chunk = done
                                    QTimer.singleShot(0, lambda d=done: self._upd_progress(-1, d, 0))
                    QTimer.singleShot(0, lambda: self._finish_update(dest))
                    return
                except Exception:
                    try:
                        os.remove(dest)
                    except Exception:
                        pass
                    continue
            QTimer.singleShot(0, lambda: self._upd_failed("网络不可用或下载失败"))
        except Exception as e:
            QTimer.singleShot(0, lambda: self._upd_failed(str(e)[:120]))

    def _upd_progress(self, pct, done, total):
        if pct >= 0:
            self._upd_bar.setValue(pct)
        self._upd_lbl.setText(
            f"正在下载新版本 {self._upd_tag} … {done // 1048576}MB / {total // 1048576}MB")

    def _finish_update(self, new_exe):
        try:
            bat = os.path.join(BASE_DIR, "update.bat")
            content = (
                "@echo off\r\n"
                f'set "APP={os.path.join(BASE_DIR, "yuketang-gui.exe")}"\r\n'
                f'set "NEW={new_exe}"\r\n'
                "taskkill /f /im yuketang-gui.exe >nul 2>&1\r\n"
                ":wait\r\n"
                "timeout /t 1 /nobreak >nul\r\n"
                'tasklist /fi "imagename eq yuketang-gui.exe" 2>nul | find /i "yuketang-gui" >nul && goto :wait\r\n'
                'del /f /q "%APP%" >nul 2>&1\r\n'
                'move /y "%NEW%" "%APP%" >nul 2>&1\r\n'
                'start "" "%APP%"\r\n'
                'del /f /q "%~f0" >nul 2>&1\r\n'
                'rd /s /q "%~dp0update" >nul 2>&1\r\n'
            )
            with open(bat, "w", encoding="gbk") as f:
                f.write(content)
            subprocess.Popen(
                ["cmd", "/c", bat],
                cwd=BASE_DIR,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.append_log_note("更新包已就绪，程序将自动重启完成安装。")
            self._upd_dlg.close()
            QApplication.instance().quit()
        except Exception as e:
            self.append_log_note(f"安装更新失败: {e}")

    def _upd_failed(self, err=""):
        if getattr(self, "_upd_dlg", None) is not None:
            self._upd_dlg.close()
        msg = QMessageBox()
        msg.setWindowTitle("更新失败")
        msg.setText(f"下载更新失败：{err}\n\n可前往 GitHub Releases 手动下载。")
        msg.addButton("前往下载", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

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
                status, st_color = ("检查中", "#5b6ee1") if self.running_mode == "检查" else ("刷课中", "#d29922")
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
            mode_color = "#5b6ee1" if getattr(self, "running_mode", "") == "检查" else "#d29922"
            self.status_lbl.setText(mode_txt)
            self.status_lbl.setStyleSheet(f"color: {mode_color}; font-size: 14px; font-weight: bold;")

    # ---------- 控制 ----------
    def launch_runner(self, extra_args, mode_label):
        """启动刷课/检查逻辑：源码环境 subprocess 调 venv python；打包后 subprocess 调自身（--auto-cli）。
        刷课逻辑运行在独立进程，与 GUI 完全隔离。"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“停止”。")
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
    ($_.Name -match 'yuketang' -and $_.CommandLine -match 'auto-cli') -or
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


def _ver_tuple(tag):
    """'v1.0.0' / '1.0.1' → (1, 0, 0)"""
    out = []
    for p in tag.lstrip("vV").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def fetch_latest_release():
    """后台查询 GitHub 最新 Release；成功返回 (tag, release_url)，失败/无网返回 None"""
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    for proxy in (None, "http://127.0.0.1:12450"):
        try:
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            else:
                opener = urllib.request.build_opener()
            req = urllib.request.Request(api, headers={"User-Agent": "yuketang-auto-check"})
            with opener.open(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
                tag = data.get("tag_name", "")
                if tag:
                    return tag, data.get("html_url", GITHUB_RELEASE_URL)
        except Exception:
            continue
    return None


def main():
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv)
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
        auto.main(args or None)
        sys.exit(0)
    main()
