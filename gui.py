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
import time
import urllib.request
import webbrowser

import auto  # 内嵌核心刷课逻辑（打包后随本程序一起分发）

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

# ---- 版本与更新检查 ----
APP_VERSION = "1.0.20"        # 每次发新版前手动递增
GITHUB_REPO = "xyUUoo/yuketang-auto"
GITHUB_RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_PY = os.path.join(BASE_DIR, "auto.py")
PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
PAUSE_FILE = os.path.join(BASE_DIR, "pause.flag")
RUN_LOCK = os.path.join(BASE_DIR, "run.lock")
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
QPushButton#pauseBtn { background-color: #d29922; color: white; }
QPushButton#pauseBtn:hover { background-color: #e0a632; }
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
QScrollBar:vertical { background: #262833; width: 12px; border-radius: 6px; }
QScrollBar::handle:vertical { background: #4a4f68; border-radius: 6px; min-height: 40px; }
QScrollBar::handle:vertical:hover { background: #5b6ee1; }
QScrollBar:horizontal { background: #262833; height: 12px; border-radius: 6px; }
QScrollBar::handle:horizontal { background: #4a4f68; border-radius: 6px; min-width: 40px; }
QScrollBar::handle:horizontal:hover { background: #5b6ee1; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


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
        # 控制栏一行放不下时按钮文字会被压成省略号：默认给足宽度（不超过屏幕可用宽度）
        try:
            avail = QGuiApplication.primaryScreen().availableGeometry()
            win_w = min(1260, int(avail.width() * 0.9))
        except Exception:
            win_w = 1260
        self.resize(max(win_w, 1000), 720)
        self.setMinimumSize(900, 560)

        self.proc = None
        self.running_mode = ""
        self.last_log_size = 0
        self.update_found.connect(self._on_update_found)
        self.upd_progress.connect(self._upd_progress)
        self.upd_finished.connect(self._finish_update)
        self.upd_error.connect(self._upd_failed)
        self.log_note.connect(self.append_log_note)

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
        # 状态文字放顶部，空间充裕，不会被挤成省略号
        self.status_lbl = QLabel("空闲")
        self.status_lbl.setStyleSheet("color: #2ea043; font-size: 14px; font-weight: bold;")
        head.addWidget(self.status_lbl)
        head.addSpacing(10)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self._open_settings)
        head.addWidget(self.settings_btn)
        self.updated_lbl = QLabel("更新：—")
        self.updated_lbl.setStyleSheet("color: #9aa0b4; font-size: 12px;")
        head.addWidget(self.updated_lbl)
        root.addLayout(head)

        # ===== 中间分栏：表格 + 日志 =====
        split = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["课程", "视频完成度", "课件完成度", "未刷", "状态", "类型分布"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        # 按像素平滑滚动，拖起来不卡手
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.table.horizontalHeader()
        # 课程列也按内容最长自适应，避免窗口拉宽时留白
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in (1, 2, 4):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, 180)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        # 最后一列（类型分布）吸收剩余宽度，表格整体填满横向空间；其余列按内容自适应
        hh.setStretchLastSection(True)
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
        ctrl.setSpacing(4)
        ctrl.setContentsMargins(0, 0, 0, 0)
        ctrl.addWidget(self._mk_label("每门条数："))
        self.lessons_box = QComboBox()
        self.lessons_box.addItems(["全部", "1", "2", "3", "5", "10"])
        self.lessons_box.setCurrentIndex(0)
        self.lessons_box.setMinimumWidth(0)
        self.lessons_box.setMaximumWidth(70)
        ctrl.addWidget(self.lessons_box)

        ctrl.addSpacing(6)
        ctrl.addWidget(self._mk_label("并行数："))
        self.parallel_box = QComboBox()
        self.parallel_box.addItems(["1", "2", "3"])
        self.parallel_box.setCurrentIndex(0)
        self.parallel_box.setMaximumWidth(56)
        ctrl.addWidget(self.parallel_box)

        ctrl.addSpacing(6)
        ctrl.addWidget(self._mk_label("课件最长等："))
        self.dwell_box = QComboBox()
        self.dwell_box.addItems(["5 秒", "10 秒", "15 秒", "20 秒", "30 秒"])
        self.dwell_box.setCurrentText("5 秒")
        self.dwell_box.setMaximumWidth(74)
        self.dwell_box.setToolTip("每页最多等这么久：页面“未读”标记一消失就自动翻下一页；视频用不到")
        ctrl.addWidget(self.dwell_box)

        ctrl.addSpacing(8)
        self.btn_start_video = QPushButton("开始刷网课视频")
        self.btn_start_video.setObjectName("startBtn")
        self.btn_start_video.setToolTip("只刷视频类未完成条目")
        self.btn_start_video.clicked.connect(lambda: self.start("video"))
        self._fit_button(self.btn_start_video)
        ctrl.addWidget(self.btn_start_video)

        ctrl.addSpacing(4)
        self.btn_start_cw = QPushButton("开始刷网课课件")
        self.btn_start_cw.setObjectName("startBtn")
        self.btn_start_cw.setToolTip("只刷课件（PPT/文档）类未完成条目，逐页翻到未读全消")
        self.btn_start_cw.clicked.connect(lambda: self.start("courseware"))
        self._fit_button(self.btn_start_cw)
        ctrl.addWidget(self.btn_start_cw)

        ctrl.addSpacing(4)
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setObjectName("pauseBtn")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self._fit_button(self.btn_pause)
        ctrl.addWidget(self.btn_pause)

        ctrl.addSpacing(4)
        self.btn_end = QPushButton("结束")
        self.btn_end.setObjectName("stopBtn")
        self.btn_end.setEnabled(False)
        self.btn_end.clicked.connect(self.end_now)
        self._fit_button(self.btn_end)
        ctrl.addWidget(self.btn_end)

        ctrl.addSpacing(4)
        self.btn_status = QPushButton("检查状态")
        self.btn_status.setObjectName("primaryBtn")
        self.btn_status.clicked.connect(self.check_status)
        self._fit_button(self.btn_status)
        ctrl.addWidget(self.btn_status)

        ctrl.addSpacing(4)
        self.btn_refresh = QPushButton("↻ 刷新")
        self.btn_refresh.clicked.connect(self.refresh_all)
        self._fit_button(self.btn_refresh)
        ctrl.addWidget(self.btn_refresh)

        ctrl.addStretch(1)
        root.addLayout(ctrl)

        # 定时刷新
        self.refresh_all()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(2000)

        # 启动 3 秒后后台检查 GitHub 是否有新版本（失败静默）
        QTimer.singleShot(3000, self._start_update_check)

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
        later_btn = msg.addButton("下次提醒", QMessageBox.ButtonRole.RejectRole)
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
        self._upd_dlg = None
        threading.Thread(target=self._download_update_worker, args=(tag,), daemon=True).start()

    def _download_update_worker(self, tag):
        self._upd_log("下载线程启动")
        try:
            url = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/yuketang-gui.exe"
            upd_dir = os.path.join(BASE_DIR, "update")
            os.makedirs(upd_dir, exist_ok=True)
            dest = os.path.join(upd_dir, "yuketang-gui.exe")
            last_pct = -1
            last_chunk = 0
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

    def _mk_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #9aa0b4; font-size: 13px;")
        return lbl

    def _fit_button(self, btn, pad=44):
        """按文字宽度设最小宽度（按 QSS 的 13px 字号量，左右各 16px 内边距 + 余量），
        保证按钮上的字完整显示、不被压成省略号"""
        btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        f = QFont(btn.font())
        f.setPixelSize(13)  # 与 QSS 中 QPushButton 的 font-size 一致
        text_w = QFontMetrics(f).horizontalAdvance(btn.text())
        btn.setMinimumWidth(text_w + pad)

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
            ks = info.get("kind_split") or {}

            item_name = QTableWidgetItem(name)
            item_name.setForeground(QColor("#e6e6e6"))
            self.table.setItem(row, 0, item_name)

            # 视频完成度 / 课件完成度：各画一条进度条（done / total）
            ts = info.get("target_split") or {}
            for col, kind in ((1, "视频"), (2, "课件")):
                d = ks.get(kind) or {}
                kt = int(d.get("total") or 0)
                kr = int(d.get("remaining") or 0)
                if not kt:
                    old_total = int(ts.get(kind) or 0)
                    txt = f"? / {old_total}" if old_total else "0 / 0"
                    it = QTableWidgetItem(txt)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    it.setForeground(QColor("#8a91a8"))
                    it.setToolTip(
                        f"本课目标里有 {old_total} 条{kind}，但还没有分类完成记录；跑一轮刷课后会显示实际完成度"
                        if old_total else f"本课暂无{kind}目标")
                    self.table.setItem(row, col, it)
                    continue
                kd = max(0, kt - kr)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(int(round(kd / kt * 100)))
                bar.setFormat(f"{kd} / {kt}")
                bar.setToolTip(f"{kind}：{kd} 已完成，{kr} 条未刷")
                bar.setStyleSheet(
                    "QProgressBar { background-color: #33364a; border: none; border-radius: 5px;"
                    " text-align: center; color: white; font-size: 11px; min-height: 14px; }"
                    "QProgressBar::chunk { border-radius: 5px; background-color: %s; }"
                    % ("#2ea043" if kr == 0 else "#5b6ee1")
                )
                self.table.setCellWidget(row, col, bar)

            # 未刷：视频/课件各剩多少条
            if ks:
                parts = []
                for kind in ("视频", "课件"):
                    d = ks.get(kind) or {}
                    if int(d.get("total") or 0):
                        parts.append(f"{kind} {int(d.get('remaining') or 0)}")
                rem_text = " · ".join(parts) if parts else "无"
                zero = bool(parts) and all(
                    int((ks.get(p.split(" ")[0]) or {}).get("remaining") or 0) == 0 for p in parts)
            else:
                rem_text = f"共 {remain} 条" if remain else ("无" if total else "—")
                zero = (remain == 0 and total > 0)
            item_rem = QTableWidgetItem(rem_text)
            item_rem.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_rem.setForeground(QColor("#2ea043" if zero else "#d29922"))
            item_rem.setToolTip("还剩多少条视频 / 课件没刷" if ks else "旧记录：只有总剩余，跑一轮刷课后会按视频/课件拆开")
            self.table.setItem(row, 3, item_rem)

            # 状态：已刷完 / 无任务 / 正在刷（仅运行中）/ 等待中
            status, st_color = "等待中", "#5b6ee1"
            if info.get("target", 0) == 0:
                status, st_color = "无任务", "#8a91a8"
            elif done:
                status, st_color = "已刷完 ✓", "#2ea043"
            elif is_running and name in running_names:
                status, st_color = ("检查中", "#5b6ee1") if self.running_mode == "检查" else (f"{self.running_mode}中", "#d29922")
            item_st = QTableWidgetItem(status)
            item_st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_st.setForeground(QColor(st_color))
            self.table.setItem(row, 4, item_st)

            # 未完成列表的类型分布（视频×N 课件×N 作业×N …），无记录显示 —
            td = info.get("type_dist") or {}
            td_text = " ".join(f"{k}×{v}" for k, v in td.items()) if td else "—"
            item_td = QTableWidgetItem(td_text)
            item_td.setToolTip(td_text)
            item_td.setForeground(QColor("#9aa0b4"))
            self.table.setItem(row, 5, item_td)

        self.table.resizeColumnsToContents()
        # 最后一列（类型分布）自适应剩余宽度，避免出现横向滚动条
        self.table.horizontalHeader().setStretchLastSection(True)

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
        # 自己启动的进程已退出
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            self.btn_end.setEnabled(False)
        ext = self._external_runner()
        if self.proc is not None:
            mode_txt = "检查中…" if getattr(self, "running_mode", "") == "检查" else f"{self.running_mode or '刷课'}中…"
            mode_color = "#5b6ee1" if getattr(self, "running_mode", "") == "检查" else "#d29922"
            self.status_lbl.setText(mode_txt)
            self.status_lbl.setStyleSheet(f"color: {mode_color}; font-size: 14px; font-weight: bold;")
        elif ext:
            # 命令行/其他窗口启动的刷课任务：照样用这三个按钮控制
            self.status_lbl.setText(f"外部任务运行中（PID {ext['pid']}）")
            self.status_lbl.setStyleSheet("color: #d29922; font-size: 14px; font-weight: bold;")
            self.btn_end.setEnabled(True)
        else:
            self.status_lbl.setText("空闲（上次运行已结束）")
            self.status_lbl.setStyleSheet("color: #2ea043; font-size: 14px; font-weight: bold;")
            self.btn_end.setEnabled(False)
        self._sync_pause_button()

    def _sync_pause_button(self):
        """暂停按钮文字/可用状态跟 pause.flag 实际情况保持一致"""
        running = (self.proc is not None and self.proc.poll() is None) or self._external_runner() is not None
        self.btn_pause.setText("继续" if os.path.exists(PAUSE_FILE) else "暂停")
        self.btn_pause.setEnabled(bool(running))

    def _external_runner(self):
        """读取 run.lock，返回仍在运行的外部刷课任务（dict），没有则 None"""
        if self.proc is not None and self.proc.poll() is None:
            return None
        try:
            with open(RUN_LOCK, "r", encoding="utf-8", errors="replace") as f:
                info = json.load(f)
        except Exception:
            return None
        pid = info.get("pid")
        if not isinstance(pid, int) or pid == os.getpid():
            return None
        try:
            import psutil
            if not psutil.pid_exists(pid):
                return None
            cmd = " ".join(psutil.Process(pid).cmdline())
            if "yuketang" not in cmd and "auto.py" not in cmd:
                return None
        except Exception:
            return None
        return info

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
        self._clear_pause()
        self.btn_pause.setText("暂停")
        self.btn_pause.setEnabled(True)
        self.btn_end.setEnabled(True)
        return True

    def start(self, kinds="video,courseware"):
        """刷课模式：kinds 决定只刷视频还是只刷课件（两个按钮各自调用）"""
        if self.proc is not None and self.proc.poll() is None:
            self.append_log_note("已有任务在运行，请先点“结束”。")
            return
        ext = self._external_runner()
        if ext:
            self.append_log_note(
                f"已有外部任务在运行（PID {ext['pid']}，{ext.get('start', '')} 启动），"
                f"可用「暂停」「结束」控制它，或先「结束」再重新开始。"
            )
            return
        if not FROZEN and not os.path.exists(AUTO_PY):
            self.append_log_note(f"找不到脚本：{AUTO_PY}")
            return
        only_txt = "视频" if kinds == "video" else ("课件" if kinds == "courseware" else "视频+课件")
        # 首次运行（还没有任何进度记录）：建议先检查状态
        if not self._has_progress():
            msg = QMessageBox()
            msg.setWindowTitle("首次运行")
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setText(
                "首次运行前建议先「检查状态」\n\n"
                "检查会刷新课程与未完成数量（视频 / 课件），避免误判“已刷完”。"
            )
            btn_check = msg.addButton("去检查状态", QMessageBox.ButtonRole.AcceptRole)
            btn_direct = msg.addButton("直接刷课", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            if msg.clickedButton() == btn_check:
                self.check_status()
                return
        n = self.lessons_box.currentText()
        pn = self.parallel_box.currentText()
        dwell = self.dwell_box.currentText().split(" ")[0]
        extra = ["--dwell", dwell, "--types", kinds]
        if n != "全部":
            extra += ["--max-lessons", n]
        if pn != "1":
            extra += ["--parallel", pn]
        label = f"刷{only_txt}模式，每门 {n} 条，课件每页最长等 {dwell}s，并行 {pn} 窗口"
        if not self.launch_runner(extra, label):
            return
        self.running_mode = f"刷{only_txt}"
        self.status_lbl.setText(f"刷{only_txt}中…")
        self.status_lbl.setStyleSheet("color: #d29922; font-size: 14px; font-weight: bold;")

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
        ext = self._external_runner()
        if ext:
            self.append_log_note(f"已有外部任务在运行（PID {ext['pid']}），请先「结束」它。")
            return
        if not FROZEN and not os.path.exists(AUTO_PY):
            self.append_log_note(f"找不到脚本：{AUTO_PY}")
            return
        if not self.launch_runner(["--quick", "--types", "video,courseware"], "检查模式：遍历刷新进度，不播放"):
            return
        self.running_mode = "检查"
        self.status_lbl.setText("检查中…")
        self.status_lbl.setStyleSheet("color: #5b6ee1; font-size: 14px; font-weight: bold;")

    def toggle_pause(self):
        """暂停/继续：写或删 pause.flag，刷课进程检测后暂停/恢复播放"""
        if os.path.exists(PAUSE_FILE):
            try:
                os.remove(PAUSE_FILE)
            except Exception:
                pass
            self.btn_pause.setText("暂停")
            self.append_log_note("已发送「继续」指令，刷课恢复。")
        else:
            try:
                with open(PAUSE_FILE, "w", encoding="utf-8") as f:
                    f.write("1")
            except Exception as e:
                self.append_log_note(f"发送暂停指令失败: {e}")
                return
            self.btn_pause.setText("继续")
            self.append_log_note("已发送「暂停」指令，当前条目暂停播放，进度不计入。")

    def end_now(self):
        """结束：彻底关闭刷课脚本 + 所有刷课用的 Edge 浏览器 + 残留进程"""
        self._clear_pause()
        ext = self._external_runner()
        if ext:
            try:
                import psutil
                p = psutil.Process(ext["pid"])
                for child in p.children(recursive=True):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                p.terminate()
                try:
                    p.wait(timeout=8)
                except Exception:
                    p.kill()
            except Exception:
                pass
            self.append_log_note(f"已结束后台任务（PID {ext['pid']}）。")
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
        3) 跑了 auto.py 的 python 残留
        注意：绝不杀控制台自己及其父进程（源码模式 gui.py 的命令行同样含 yuketang）"""
        try:
            import psutil
        except Exception:
            self._run_cleanup_ps()
            return
        protected = {os.getpid()}
        try:
            protected |= {p.pid for p in psutil.Process().parents()}
        except Exception:
            pass
        killed = []
        try:
            for proc in psutil.process_iter(["name", "cmdline"]):
                if proc.pid in protected:
                    continue
                try:
                    name = (proc.info["name"] or "").lower()
                    cmd = " ".join(proc.info["cmdline"] or [])
                except Exception:
                    continue
                if not cmd:
                    continue
                hit = False
                if "yuketang" in name and ("--auto-cli" in cmd or "--multiprocessing-fork" in cmd):
                    hit = True
                elif name == "msedge.exe" and "edge_profile" in cmd:
                    hit = True
                elif name.startswith("python") and "auto.py" in cmd:
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
        try:
            if os.path.exists(RUN_LOCK):
                os.remove(RUN_LOCK)
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


class SettingsDialog(QDialog):
    """设置窗口：可修改检查更新/下载使用的代理地址"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(430, 170)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        lbl = QLabel("检查更新 / 下载使用的代理地址：")
        lbl.setStyleSheet("color: #e6e6e6; font-size: 13px;")
        self.proxy_edit = QLineEdit(load_settings().get("proxy") or DEFAULT_PROXY)
        self.proxy_edit.setPlaceholderText("例如 http://127.0.0.1:12450")
        tip = QLabel("留空并保存 = 仅直连 GitHub（不使用代理）")
        tip.setStyleSheet("color: #8a91a8; font-size: 12px;")
        btn_row = QHBoxLayout()
        check_btn = QPushButton("检查更新")
        check_btn.setToolTip("立即用当前代理设置检查 GitHub 新版本（保存代理后生效）")
        check_btn.clicked.connect(self._check_now)
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(check_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        lay.addWidget(lbl)
        lay.addWidget(self.proxy_edit)
        lay.addWidget(tip)
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
