"""Qt desktop interface for Android ROM Toolkit.

The old interface was built from Tk widgets.  This module keeps the same
controller and worker protocol while using Qt's native window/backing-store
pipeline, which avoids the black repaint artefacts and resize stalls seen on
some Windows DWM configurations.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, QEvent, QPoint
from PySide6.QtGui import QAction, QIcon, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QHeaderView,
)

from Scripts.application import ArtController, ROOT
from Scripts.Platform.runtime import CAPABILITIES, process_options

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FORMAT_NAMES = {"ext": "EXT4", "erofs": "EROFS", "sparse": "Sparse", "boot": "Boot",
                "vendor_boot": "Vendor boot", "payload": "Payload", "super": "Super",
                "dat": "DAT", "dat.br": "DAT.BR", "win": "WIN", "zip": "ROM ZIP",
                "unknown": "未识别"}
STATE_NAMES = {"new": "可用", "incomplete": "未初始化", "unsupported": "旧版布局",
               "invalid": "无效", "empty": "空工程"}


def size_text(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


class EventBridge(QObject):
    event = Signal(dict)


class TitleBar(QFrame):
    """Compact title bar drawn by Qt instead of the Windows frame.

    The application still uses a regular top-level window and the parent
    installs an application event filter for edge resizing.  Keeping the title
    bar as a normal widget means it is rendered by the same backing store as
    the rest of the page, so there is no second native frame to flash while
    the window is resized.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setObjectName("titlebar")
        self.setFixedHeight(48)
        self._drag_offset = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 8, 0)
        layout.setSpacing(10)
        icon = QLabel()
        icon.setObjectName("titleIcon")
        icon.setFixedSize(24, 24)
        icon_path = window._icon_path()
        if icon_path:
            pixmap = QPixmap(str(icon_path)).scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon.setPixmap(pixmap)
        layout.addWidget(icon)
        title = QLabel("Android ROM Toolkit")
        title.setObjectName("windowTitle")
        layout.addWidget(title)
        subtitle = QLabel("Windows 工作台")
        subtitle.setObjectName("windowSubtitle")
        layout.addWidget(subtitle)
        layout.addStretch(1)
        self.minimize = self._button("—", "最小化", "windowMin")
        self.maximize = self._button("□", "最大化", "windowMax")
        self.close_button = self._button("×", "关闭", "windowClose")
        self.minimize.clicked.connect(window.showMinimized)
        self.maximize.clicked.connect(window._toggle_maximize)
        self.close_button.clicked.connect(window.close)
        layout.addWidget(self.minimize)
        layout.addWidget(self.maximize)
        layout.addWidget(self.close_button)

    @staticmethod
    def _button(text, tip, object_name):
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setToolTip(tip)
        button.setFixedSize(38, 32)
        button.setFlat(True)
        return button

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window._toggle_maximize()
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            # Restore a maximized window when the user starts dragging its
            # title bar, preserving the pointer's horizontal position.
            if self.window.isMaximized():
                pointer = event.globalPosition().toPoint()
                self.window.showNormal()
                self._drag_offset = QPoint(self.window.width() // 2, 20)
                self.window.move(pointer - self._drag_offset)
            else:
                self.window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class ArtWindow(QMainWindow):
    def __init__(self, controller: ArtController):
        super().__init__()
        # A Qt-drawn frame avoids the native title bar's second composition
        # surface.  The edge resize handler below keeps normal desktop window
        # behavior without relying on platform-specific non-client painting.
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.controller = controller
        self.events: queue.Queue = queue.Queue()
        self.process = None
        # Synchronous controller helpers (import/archive/tool bootstrap) run
        # in a Python thread rather than the worker subprocess.  Keep a
        # separate guard so a second job cannot start while one of these is
        # still publishing files.
        self._thread_busy = False
        self.cancelled = False
        self.projects = []
        self.inputs = {}
        self.partitions = {}
        self.current_page = 0
        self._log_lines = 0
        self._action_buttons = []
        self.ui_theme = self._load_theme()
        self.setWindowTitle("Android ROM Toolkit for Windows")
        self.setMinimumSize(1040, 700)
        self.resize(1240, 800)
        self._set_icon()
        self._resize_margin = 7
        self._resize_mode = None
        self._resize_start_pos = None
        self._resize_start_geometry = None
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._apply_style()
        self._build_ui()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(80)

    def _set_icon(self):
        for path in (self._icon_path(),):
            if path.is_file():
                self.setWindowIcon(QIcon(str(path)))
                QApplication.instance().setWindowIcon(QIcon(str(path)))
                return

    @staticmethod
    def _icon_path():
        root = Path(getattr(sys, "_MEIPASS", ROOT))
        for path in (root / "assets" / "android-rom-toolkit.ico",
                     ROOT / "assets" / "android-rom-toolkit.ico",
                     ROOT / "art-res" / "android-rom-toolkit.ico"):
            if path.is_file():
                return path
        return root / "assets" / "android-rom-toolkit.ico"

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self.titlebar.maximize.setText("□")
            self.titlebar.maximize.setToolTip("最大化")
        else:
            self.showMaximized()
            self.titlebar.maximize.setText("❐")
            self.titlebar.maximize.setToolTip("还原")

    def eventFilter(self, watched, event):
        """Provide native-like edge resizing for the frameless window.

        The filter is installed on QApplication so it also receives mouse
        events delivered to child widgets.  Geometry is changed directly from
        the original rectangle and pointer delta, avoiding a layout rebuild on
        every move and keeping resize repainting smooth.
        """
        # QApplication sees events for file dialogs and other top-level
        # windows as well.  Only handle events belonging to this window's
        # widget tree; otherwise opening a native dialog could accidentally
        # begin a resize because its screen coordinates map outside our frame.
        in_window = watched is self or (isinstance(watched, QWidget) and self.isAncestorOf(watched))
        if in_window and event.type() in (QEvent.Type.MouseButtonPress,
                                          QEvent.Type.MouseMove,
                                          QEvent.Type.MouseButtonRelease):
            if self.isMaximized() or not self.isVisible():
                return super().eventFilter(watched, event)
            point = event.globalPosition().toPoint()
            local = self.mapFromGlobal(point)
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                mode = self._resize_hit_test(local)
                if mode:
                    self._resize_mode = mode
                    self._resize_start_pos = point
                    self._resize_start_geometry = self.geometry()
                    return True
            elif event.type() == QEvent.Type.MouseMove and self._resize_mode:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    self._resize_by_delta(point)
                    return True
                self._resize_mode = None
            elif event.type() == QEvent.Type.MouseButtonRelease and self._resize_mode:
                self._resize_by_delta(point)
                self._resize_mode = None
                return True
        return super().eventFilter(watched, event)

    def _resize_hit_test(self, point):
        margin = self._resize_margin
        x, y, width, height = point.x(), point.y(), self.width(), self.height()
        left, right = x <= margin, x >= width - margin
        top, bottom = y <= margin, y >= height - margin
        if top and left: return "tl"
        if top and right: return "tr"
        if bottom and left: return "bl"
        if bottom and right: return "br"
        if left: return "l"
        if right: return "r"
        if top: return "t"
        if bottom: return "b"
        return None

    def _resize_by_delta(self, point):
        if self._resize_start_geometry is None or self._resize_start_pos is None:
            return
        delta = point - self._resize_start_pos
        geometry = self._resize_start_geometry
        left, top, width, height = geometry.x(), geometry.y(), geometry.width(), geometry.height()
        min_width, min_height = self.minimumWidth(), self.minimumHeight()
        mode = self._resize_mode or ""
        if "l" in mode:
            new_left = min(left + delta.x(), left + width - min_width)
            width += left - new_left; left = new_left
        if "r" in mode:
            width = max(min_width, width + delta.x())
        if "t" in mode:
            new_top = min(top + delta.y(), top + height - min_height)
            height += top - new_top; top = new_top
        if "b" in mode:
            height = max(min_height, height + delta.y())
        self.setGeometry(left, top, width, height)

    def _apply_style(self):
        # Fusion uses Qt's cross-platform controls and lets DWM composite one
        # backing store instead of hundreds of Tk child windows.
        QApplication.instance().setStyle("Fusion")
        sheet = """
            * { font-family: 'Microsoft YaHei UI'; font-size: 10pt; }
            QWidget { color: #142238; }
            QMainWindow, QWidget#root, QWidget#windowBody, QStackedWidget { background: #eef3fb; }
            QFrame#titlebar { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #e6edff, stop:0.52 #f6f8ff, stop:1 #e6f8f6); border-bottom: 1px solid #d4e0f2; }
            QLabel#windowTitle { font-size: 11pt; font-weight: 700; color: #17253c; }
            QLabel#windowSubtitle { color: #6e7d95; font-size: 9pt; }
            QLabel#titleIcon { background: transparent; }
            QPushButton#windowMin, QPushButton#windowMax, QPushButton#windowClose { border: 0; border-radius: 7px; background: transparent; color: #52627a; font-size: 14pt; padding: 0; }
            QPushButton#windowMin:hover, QPushButton#windowMax:hover { background: rgba(77, 112, 172, 35); color: #1e3a63; }
            QPushButton#windowClose:hover { background: #df5d78; color: white; }
            QFrame#sidebar { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #e7edfb, stop:0.48 #e9f4ff, stop:1 #e2f5f2); border-right: 1px solid #d5e0f0; }
            QFrame#card, QGroupBox { background: rgba(255,255,255,218); border: 1px solid rgba(196,211,232,210); border-radius: 14px; }
            QGroupBox { margin-top: 12px; padding: 18px 12px 12px 12px; }
            QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 7px; font-weight: 700; color: #1c3356; background: #eef3fb; }
            QLabel#muted { color: #6c7d96; }
            QLabel#title { font-size: 20pt; font-weight: 750; color: #14294a; }
            QPushButton { border: 1px solid #c4d1e5; border-radius: 8px; padding: 8px 15px; background: rgba(255,255,255,235); color: #1c355b; }
            QPushButton:hover { background: #e6efff; border-color: #638bd2; }
            QPushButton#primary { color: white; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3c70d6, stop:1 #517fde); border-color: #3c70d6; font-weight: 650; }
            QPushButton#primary:hover { background: #315dbb; }
            QPushButton:disabled { color: #9daabe; background: #e5ebf4; }
            QListWidget#nav { background: transparent; border: 0; outline: 0; }
            QListWidget#nav::item { padding: 12px 14px; margin: 4px 0; border-radius: 9px; color: #60718d; }
            QListWidget#nav::item:hover { background: rgba(255,255,255,135); color: #274774; }
            QListWidget#nav::item:selected { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #d4e3ff, stop:1 #dff4f3); color: #173c70; font-weight: 700; }
            QLineEdit, QComboBox, QSpinBox { background: rgba(255,255,255,235); border: 1px solid #c4d1e5; border-radius: 7px; padding: 6px; selection-background-color: #8eb2ee; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #638bd2; }
            QTableWidget { background: rgba(255,255,255,220); alternate-background-color: #f3f7fd; border: 1px solid #cedbeb; border-radius: 9px; gridline-color: #e3eaf3; }
            QHeaderView::section { background: #e6edf7; padding: 8px; border: 0; font-weight: 650; color: #365174; }
            QPlainTextEdit { background: #f2f6fb; border: 1px solid #cedbeb; border-radius: 8px; font-family: Consolas; }
            QProgressBar { border: 0; background: #dfe8f4; border-radius: 5px; height: 9px; text-visible: false; }
            QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3d73d9, stop:1 #51b6c3); border-radius: 5px; }
            QTabWidget::pane { border: 1px solid #d2deee; border-radius: 10px; background: rgba(255,255,255,110); top: -1px; }
            QTabBar::tab { background: transparent; color: #6d7d95; padding: 9px 16px; margin-right: 3px; border-radius: 8px; }
            QTabBar::tab:hover { background: #e6effe; }
            QTabBar::tab:selected { background: #d6e5ff; color: #204a85; font-weight: 700; }
            QCheckBox { spacing: 7px; color: #51647f; }
            QSplitter::handle { background: #d4dfed; }
        """
        if self.ui_theme == "dark":
            sheet += """
                QWidget { color: #ecf2fc; }
                QMainWindow, QWidget#root, QWidget#windowBody, QStackedWidget { background: #101721; }
                QFrame#titlebar { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #192743, stop:0.52 #1d2638, stop:1 #173736); border-color: #2f4463; }
                QLabel#windowTitle { color: #edf4ff; } QLabel#windowSubtitle { color: #9eafc9; }
                QPushButton#windowMin, QPushButton#windowMax, QPushButton#windowClose { color: #b9c9e1; }
                QPushButton#windowMin:hover, QPushButton#windowMax:hover { background: rgba(120, 160, 230, 55); color: #fff; }
                QFrame#sidebar { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #172239, stop:0.5 #15283a, stop:1 #143331); border-color: #2b3e57; }
                QFrame#card, QGroupBox, QTableWidget, QLineEdit, QComboBox, QSpinBox { background: rgba(26,37,55,230); border-color: #334962; }
                QGroupBox::title { background: #101721; color: #d5e3fb; }
                QLabel#muted { color: #95a8c3; }
                QLabel#title { color: #edf4ff; }
                QPushButton { background: #1a2638; color: #ecf2fc; border-color: #3c526f; }
                QPushButton:hover { background: #263d60; }
                QListWidget#nav::item:selected { background: #294467; color: #f4f8ff; }
                QPlainTextEdit { background: #101925; color: #cbd9ef; border-color: #33415a; }
                QTableWidget { alternate-background-color: #1d293d; gridline-color: #2a3850; }
                QHeaderView::section { background: #202e44; color: #c8d8ef; }
                QTabBar::tab:selected { background: #294a72; color: #eff6ff; }
                QTabWidget::pane { border-color: #324860; background: rgba(20,31,46,140); }
                QCheckBox { color: #aebed5; }
                QSplitter::handle { background: #2f4158; }
            """
        QApplication.instance().setStyleSheet(sheet)

    def _settings_path(self):
        path = self.controller.root / "art-res" / "ui-settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_theme(self):
        try:
            value = json.loads(self._settings_path().read_text(encoding="utf-8")).get("theme", "light")
            return value if value in {"light", "dark"} else "light"
        except (OSError, ValueError, TypeError):
            return "light"

    def _save_theme(self):
        try:
            self._settings_path().write_text(json.dumps({"theme": self.ui_theme}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _change_theme(self, value):
        self.ui_theme = "dark" if value == "深色" else "light"
        self._save_theme()
        self._apply_style()

    def _build_ui(self):
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.titlebar = TitleBar(self)
        outer.addWidget(self.titlebar)
        body = QWidget(objectName="windowBody")
        shell = QHBoxLayout(body)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        outer.addWidget(body, 1)
        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(225)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(20, 28, 20, 20)
        brand = QLabel("▣  A.R.T")
        brand.setStyleSheet("font-size: 19pt; font-weight: 700; color: #234a83;")
        side_layout.addWidget(brand)
        sub = QLabel("ANDROID ROM TOOLKIT")
        sub.setObjectName("muted")
        side_layout.addWidget(sub)
        side_layout.addSpacing(22)
        self.nav = QListWidget(objectName="nav")
        self.nav.addItems(["工作台", "镜像处理", "工具链", "MCP 连接"])
        self.nav.currentRowChanged.connect(self._show_page)
        side_layout.addWidget(self.nav)
        side_layout.addStretch()
        foot = QLabel("Windows · Linux\n桌面界面  /  CLI  /  MCP")
        foot.setObjectName("muted")
        side_layout.addWidget(foot)
        shell.addWidget(sidebar)
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(28, 22, 28, 18)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        self.page_title = QLabel("工作台", objectName="title")
        self.page_subtitle = QLabel("管理 ROM 工程，保留每次构建的输入、工作区和产物", objectName="muted")
        titles.addWidget(self.page_title); titles.addWidget(self.page_subtitle)
        header.addLayout(titles); header.addStretch()
        header.addWidget(QLabel("当前工程"))
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(190)
        self.project_combo.currentTextChanged.connect(self._project_changed)
        header.addWidget(self.project_combo)
        ml.addLayout(header)
        self.pages = QStackedWidget()
        ml.addWidget(self.pages, 1)
        self.status = QLabel("就绪", objectName="muted")
        ml.addWidget(self.status)
        shell.addWidget(main, 1)
        self._workspace_page(); self._images_page(); self._runtime_page(); self._mcp_page()
        self.nav.setCurrentRow(0)

    def _card(self, title, subtitle=""):
        box = QGroupBox(title, objectName="card")
        lay = QVBoxLayout(box)
        if subtitle:
            label = QLabel(subtitle, objectName="muted")
            lay.addWidget(label)
        return box, lay

    def _table(self, headers, select=QTableWidget.ExtendedSelection):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(select)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        return table

    def _workspace_page(self):
        page = QWidget(); layout = QVBoxLayout(page)
        stats = QHBoxLayout(); self.stat_labels = []
        for title, detail in (("工程", "INPUT / WORKSPACE / OUT"), ("输入文件", "原始镜像与 ROM 文件"), ("产物", "已完成的输出文件")):
            box, bl = self._card(title, detail); value = QLabel("0"); value.setStyleSheet("font-size: 24pt; font-weight: 700; color:#376fd1;")
            bl.addWidget(value); stats.addWidget(box); self.stat_labels.append(value)
        layout.addLayout(stats)
        splitter = QSplitter(Qt.Horizontal)
        box, bl = self._card("工程列表", "双击工程进入镜像处理")
        self.project_table = self._table(["工程", "状态", "输入", "产物"], QTableWidget.SingleSelection)
        self.project_table.itemSelectionChanged.connect(self._select_project)
        self.project_table.cellDoubleClicked.connect(lambda *_: self.nav.setCurrentRow(1))
        bl.addWidget(self.project_table)
        row = QHBoxLayout(); b = self._button("刷新", self.refresh); row.addWidget(b); row.addStretch(); row.addWidget(self._button("打开工程目录", self._open_project)); bl.addLayout(row)
        splitter.addWidget(box)
        box2, bl2 = self._card("新建工程", "独立目录便于保留原始输入和每次构建的工作现场")
        self.new_name = QLineEdit(); self.new_name.setPlaceholderText("例如 DNA_MAYFLY_ORIGIN")
        bl2.addWidget(QLabel("工程名称", objectName="muted")); bl2.addWidget(self.new_name); bl2.addWidget(self._button("创建工程", self._create_project, True)); bl2.addSpacing(12)
        bl2.addWidget(QLabel("快速开始\n01 创建或选择工程\n02 导入镜像文件\n03 提取后编辑工作区\n04 回包并检查产物", objectName="muted")); bl2.addStretch()
        splitter.addWidget(box2); splitter.setSizes([700, 300]); layout.addWidget(splitter, 1)
        self.pages.addWidget(page)

    def _images_page(self):
        page = QWidget(); layout = QVBoxLayout(page)
        box, bl = self._card("镜像与分区", "Payload / super 提取到 OUT；文件系统镜像提取到 WORKSPACE")
        top = QHBoxLayout(); top.addWidget(self._button("导入文件", self._import_files, True)); top.addWidget(self._button("导入 ROM ZIP", self._import_archive)); top.addWidget(self._button("刷新", self.refresh_inputs)); top.addStretch(); top.addWidget(self._button("打开输出", lambda: self._open_project("OUT"))); bl.addLayout(top)
        tabs = QTabWidget(); bl.addWidget(tabs, 1)
        input_tab = QWidget(); il = QVBoxLayout(input_tab)
        self.input_table = self._table(["文件", "格式", "大小"]); il.addWidget(self.input_table)
        row = QHBoxLayout(); row.addWidget(self._button("提取所选", self._extract, True)); self.deep = QCheckBox("继续解包 Payload / super 中的 IMG"); self.deep.setChecked(True); row.addWidget(self.deep); row.addWidget(self._button("转为 Sparse", lambda: self._convert("sparse"))); row.addWidget(self._button("转为 RAW", lambda: self._convert("raw"))); il.addLayout(row)
        fmt = QHBoxLayout(); fmt.addWidget(QLabel("按类型解包", objectName="muted"))
        for label, value in (("解包 IMG", "img"), ("解包 Payload", "payload"), ("解包 DAT", "dat"), ("解包 DAT.BR", "dat.br"), ("解包 WIN", "win"), ("解包 super", "super")):
            fmt.addWidget(self._button(label, lambda checked=False, v=value: self._extract_format(v)))
        fmt.addStretch(); il.addLayout(fmt); tabs.addTab(input_tab, "输入文件")
        part_tab = QWidget(); pl = QVBoxLayout(part_tab)
        self.partition_table = self._table(["分区", "原文件系统"]); pl.addWidget(self.partition_table)
        row = QHBoxLayout(); row.addWidget(self._button("回包所选分区", self._repack, True)); row.addWidget(self._button("打开工作区", lambda: self._open_project("WORKSPACE"))); row.addStretch(); row.addWidget(QLabel("回包格式")); self.repack_target = QComboBox(); self.repack_target.addItems(["IMG", "DAT", "DAT.BR"]); row.addWidget(self.repack_target); self.sparse = QCheckBox("输出 Sparse"); row.addWidget(self.sparse); pl.addLayout(row); tabs.addTab(part_tab, "工作区分区")
        super_tab = QWidget(); sl = QVBoxLayout(super_tab)
        self.super_table = self._table(["镜像", "来源", "大小"]); sl.addWidget(self.super_table)
        row = QHBoxLayout(); row.addWidget(self._button("合成 super.img", self._repack_super, True)); row.addStretch(); row.addWidget(QLabel("类型")); self.super_type = QComboBox(); self.super_type.addItems(["A-only", "A/B", "Virtual A/B"]); row.addWidget(self.super_type); self.super_sparse = QCheckBox("Sparse 输出"); row.addWidget(self.super_sparse); sl.addLayout(row); tabs.addTab(super_tab, "合成 super")
        layout.addWidget(box, 1)
        logbox, ll = self._card("任务日志"); prog = QHBoxLayout(); self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide(); prog.addWidget(self.progress, 1); self.cancel_btn = self._button("取消", self._cancel); self.cancel_btn.setEnabled(False); prog.addWidget(self.cancel_btn); ll.addLayout(prog); self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(20000); self.log.setMinimumHeight(130); ll.addWidget(self.log); layout.addWidget(logbox)
        self.pages.addWidget(page)

    def _runtime_page(self):
        page = QWidget(); layout = QVBoxLayout(page)
        box, bl = self._card("界面外观", "Qt 使用系统级窗口合成；浅色主题为默认")
        row = QHBoxLayout(); row.addWidget(QLabel("主题")); self.theme = QComboBox(); self.theme.addItems(["浅色", "深色"]); self.theme.setCurrentText("深色" if self.ui_theme == "dark" else "浅色"); self.theme.currentTextChanged.connect(self._change_theme); row.addWidget(self.theme); row.addStretch(); bl.addLayout(row); layout.addWidget(box)
        box, bl = self._card("运行后端", "Windows 原生无需 WSL；缺失工具可自动补齐")
        row = QHBoxLayout(); self.backend = QComboBox(); self.backend.addItems(["native", "wsl"]); self.tool_dir = QLineEdit(); row.addWidget(self.backend); row.addWidget(self.tool_dir, 1); row.addWidget(self._button("浏览", self._browse_tools)); row.addWidget(self._button("保存配置", self._save_tools, True)); bl.addLayout(row); self.backend_label = QLabel(objectName="muted"); bl.addWidget(self.backend_label); bl.addWidget(self._button("自动补齐 Windows 工具", self._bootstrap_tools)); layout.addWidget(box)
        box, bl = self._card("功能可用性", "可用状态依据工具文件检测；实际命令执行结果以任务日志为准")
        self.capability_table = self._table(["功能", "状态", "工具"]); bl.addWidget(self.capability_table); layout.addWidget(box, 1); self.pages.addWidget(page)
        box, bl = self._card("CLI 高级设置", "与原版命令行菜单共用 settings.json")
        form = QGridLayout(); self.cli_settings = {}
        setting_defs = (("REPACK_EROFS_IMG", "镜像类型", ("0 · EXT4", "1 · EROFS")),
                        ("REPACK_SPARSE_IMG", "镜像格式", ("0 · RAW", "1 · Sparse")),
                        ("REPACK_TO_RW", "EXT4 动态分区", ("0 · RO", "1 · RW")),
                        ("RESIZE_IMG", "EXT4 压缩空间", ("0 · 否", "1 · 是")),
                        ("RESIZE_EROFSIMG", "EROFS 压缩算法", ("0 · 无", "1 · LZ4HC", "2 · LZ4")),
                        ("EROFS_LEVEL", "EROFS 压缩等级", tuple(str(i) for i in range(1, 13))),
                        ("REPACK_BR_LEVEL", "BROTLI 等级", tuple(str(i) for i in range(10))),
                        ("UNPACK_SPLIT_DAT", "DAT 分段数", ("5", "10", "15", "20", "30")))
        values = self.controller.get_settings()
        for index, (key, title, choices) in enumerate(setting_defs):
            row, col = divmod(index, 2); form.addWidget(QLabel(title, objectName="muted"), row, col * 2)
            combo = QComboBox(); combo.addItems(list(choices)); current = str(values.get(key, ""))
            for choice_index, choice in enumerate(choices):
                if choice.startswith(current + " ") or choice == current:
                    combo.setCurrentIndex(choice_index); break
            combo.setProperty("settingKey", key); self.cli_settings[key] = combo; form.addWidget(combo, row, col * 2 + 1)
        bl.addLayout(form); bl.addWidget(self._button("保存 CLI 设置", self._save_cli_settings, True)); layout.addWidget(box)

    def _save_cli_settings(self):
        updates = {}
        for key, combo in self.cli_settings.items():
            value = combo.currentText().split(" ", 1)[0]
            updates[key] = value
        try:
            self.controller.update_settings(updates)
            self.status.setText("CLI 设置已保存")
        except Exception as error:
            QMessageBox.critical(self, "保存设置失败", str(error))

    def _mcp_page(self):
        page = QWidget(); layout = QVBoxLayout(page); box, bl = self._card("连接你的 AI 客户端", "复制配置到支持 MCP stdio 的客户端")
        command = [sys.executable, "--mcp", "--root", str(self.controller.root)] if getattr(sys, "frozen", False) else [sys.executable, str(ROOT / "Scripts" / "main.py"), "--mcp", "--root", str(self.controller.root)]
        self.mcp_config = json.dumps({"mcpServers": {"art": {"command": command[0], "args": command[1:]}}}, ensure_ascii=False, indent=2)
        edit = QPlainTextEdit(self.mcp_config); edit.setReadOnly(True); bl.addWidget(edit); bl.addWidget(self._button("复制连接配置", lambda: self._copy_config(edit), True)); bl.addWidget(QLabel("可调用工具\n工程创建 / 列表 · 输入识别 / 导入\n镜像提取 · 分区回包 · RAW / Sparse 转换\n工具链诊断 · 工作区分区列表", objectName="muted")); layout.addWidget(box, 1); self.pages.addWidget(page)

    def _button(self, text, slot, primary=False):
        b = QPushButton(text); b.setObjectName("primary" if primary else "secondary"); b.clicked.connect(slot)
        self._action_buttons.append(b)
        return b

    def _show_page(self, index):
        if index < 0: return
        self.current_page = index; self.pages.setCurrentIndex(index)
        titles = [("工作台", "管理 ROM 工程，保留每次构建的输入、工作区和产物"), ("镜像处理", "按原文件系统解包与回包，任务日志实时可见"), ("工具链", "检测依赖并配置 Windows 原生工具或 WSL"), ("MCP 连接", "让支持 MCP 的客户端调用同一套工程与镜像操作")]
        self.page_title.setText(titles[index][0]); self.page_subtitle.setText(titles[index][1])
        if index == 1: self.refresh_inputs()

    def _project_changed(self, _):
        self.refresh_inputs()

    def refresh(self):
        previous_project = self.project_combo.currentText()
        try: self.projects = self.controller.list_projects()
        except Exception as e: self.status.setText(str(e)); return
        self.project_table.setRowCount(0)
        for item in self.projects:
            row = self.project_table.rowCount(); self.project_table.insertRow(row)
            for col, value in enumerate((item["name"], STATE_NAMES.get(item["state"], item["state"]), item["input_count"], item["output_count"])): self.project_table.setItem(row, col, QTableWidgetItem(str(value)))
        names = [p["name"] for p in self.projects if p["state"] == "new"]
        self.project_combo.blockSignals(True); self.project_combo.clear(); self.project_combo.addItems(names); self.project_combo.blockSignals(False)
        if names:
            self.project_combo.setCurrentText(previous_project if previous_project in names else names[0])
        for label, value in zip(self.stat_labels, (len(self.projects), sum(p["input_count"] for p in self.projects), sum(p["output_count"] for p in self.projects))): label.setText(str(value))
        self.refresh_inputs(); self.refresh_runtime()

    def refresh_inputs(self):
        for table in (self.input_table, self.partition_table, self.super_table): table.setRowCount(0)
        self.inputs = {}; self.partitions = {}
        project = self.project_combo.currentText()
        if not project: return
        try:
            for i, item in enumerate(self.controller.list_inputs(project)):
                self.inputs[str(i)] = item; row = self.input_table.rowCount(); self.input_table.insertRow(row)
                for col, value in enumerate((item["name"], FORMAT_NAMES.get(item["format"], item["format"]), size_text(item["size"]))): self.input_table.setItem(row, col, QTableWidgetItem(str(value)))
                if item["name"].lower().endswith(".img"): self._add_super(item["name"], "INPUT", item["size"])
            for item in self.controller.list_partitions(project):
                self.partitions[item["name"]] = item; row = self.partition_table.rowCount(); self.partition_table.insertRow(row); self.partition_table.setItem(row, 0, QTableWidgetItem(item["name"])); self.partition_table.setItem(row, 1, QTableWidgetItem(FORMAT_NAMES.get(item["format"], item["format"])))
            for item in self.controller.list_outputs(project):
                if item["name"].lower().endswith(".img"): self._add_super(item["name"], "OUT", item["size"])
        except Exception as e: self.status.setText(str(e))

    def _add_super(self, name, source, size):
        row = self.super_table.rowCount(); self.super_table.insertRow(row)
        for col, value in enumerate((name, source, size_text(size))): self.super_table.setItem(row, col, QTableWidgetItem(str(value)))

    def refresh_runtime(self):
        try: status = self.controller.toolchain_status()
        except Exception as e: self.backend_label.setText(str(e)); return
        self.backend.setCurrentText("wsl" if status["mode"] == "wsl" else "native")
        config = self.controller.root / "art-res" / "host-tools.local.json"
        try:
            loaded = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
            local = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError, TypeError):
            local = {}
        self.tool_dir.setText(os.environ.get("ART_WINDOWS_TOOLS") or local.get("windows_tools", "")); self.backend_label.setText(f"{status['label']}  ·  {len(status['tools']) - len(status['missing'])}/{len(status['tools'])} 个外部工具已找到")
        self.capability_table.setRowCount(0)
        for name, ready in status["capabilities"].items():
            row = self.capability_table.rowCount(); self.capability_table.insertRow(row); values = (name, "可用" if ready else "缺少工具", " / ".join(CAPABILITIES[name]) or "内置 Python 实现")
            for col, value in enumerate(values): self.capability_table.setItem(row, col, QTableWidgetItem(value))

    def _require_project(self):
        project = self.project_combo.currentText()
        if not project: QMessageBox.information(self, "选择工程", "请先创建或选择工程。"); return None
        return project

    def _create_project(self):
        try: self.controller.create_project(self.new_name.text()); self.new_name.clear(); self.refresh()
        except Exception as e: QMessageBox.critical(self, "创建工程失败", str(e))

    def _select_project(self):
        row = self.project_table.currentRow()
        if row >= 0: self.project_combo.setCurrentText(self.project_table.item(row, 0).text())

    def _import_files(self):
        project = self._require_project(); paths, _ = QFileDialog.getOpenFileNames(self, "导入镜像及 transfer.list")
        if project and paths: self._thread_call(lambda: self.controller.import_inputs(project, paths), "正在导入文件…")

    def _import_archive(self):
        project = self._require_project(); path, _ = QFileDialog.getOpenFileName(self, "选择 ROM ZIP", filter="ROM ZIP (*.zip);;所有文件 (*.*)")
        if project and path: self._thread_call(lambda: self.controller.import_rom_archive(project, path), "正在导入 ROM ZIP…")

    def _selected_inputs(self):
        return [self.inputs[str(i.row())]["path"] for i in self.input_table.selectionModel().selectedRows() if str(i.row()) in self.inputs]

    def _extract(self):
        project = self._require_project(); rows = self.input_table.selectionModel().selectedRows(); paths = self._selected_inputs()
        if not project or not paths:
            return
        # Match the CLI's selective selectors when the chosen input is a
        # payload or super container.  Mixed selections stay batch based.
        if len(rows) == 1:
            item = self.inputs.get(str(rows[0].row()))
            if item and item.get("format") in {"payload", "super"}:
                self._extract_format(item["format"])
                return
        self._start("extract", project=project, sources=paths, deep=self.deep.isChecked())

    def _extract_format(self, requested):
        project = self._require_project(); maps = {"img": {"ext", "erofs", "sparse", "boot", "vendor_boot"}, "payload": {"payload"}, "dat": {"dat"}, "dat.br": {"dat.br"}, "win": {"win"}, "super": {"super"}}
        selected = [self.inputs[str(i.row())] for i in self.input_table.selectionModel().selectedRows() if str(i.row()) in self.inputs]; matches = [x for x in selected if x["format"] in maps[requested]] or [x for x in self.inputs.values() if x["format"] in maps[requested]]
        if not project or not matches:
            return
        # The CLI lets users extract only selected logical partitions from a
        # payload/super container.  Ask for that selection here; other input
        # formats keep the normal batch behavior.
        if requested in {"payload", "super"} and len(matches) == 1:
            source = matches[0]["path"]
            try:
                if requested == "payload":
                    entries = self.controller.payload_partitions(project, source)
                    names = self._choose_partitions("选择 Payload 分区", entries,
                                                    lambda item: f"{item['name']}  ·  {size_text(item.get('size', 0))}")
                    if not names:
                        if names == []:
                            QMessageBox.information(self, "选择分区", "请至少选择一个 Payload 分区。")
                        return
                    self._start("extract", project=project, sources=[source], deep=self.deep.isChecked(), payload_partitions=names)
                else:
                    entries = self.controller.super_partitions(project, source)
                    names = self._choose_partitions("选择 super 逻辑分区", entries, str)
                    if not names:
                        if names == []:
                            QMessageBox.information(self, "选择分区", "请至少选择一个 super 逻辑分区。")
                        return
                    self._start("extract", project=project, sources=[source], deep=self.deep.isChecked(), super_partitions=names)
            except Exception as error:
                QMessageBox.critical(self, "读取分区失败", str(error))
            return
        self._start("extract", project=project, sources=[x["path"] for x in matches], deep=self.deep.isChecked())

    def _choose_partitions(self, title, entries, label):
        """Return checked partition names, or None when the dialog is cancelled."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(430, 420)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("请选择要提取的分区（默认全选）", objectName="muted"))
        listing = QListWidget()
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else str(entry)
            item = QListWidgetItem(label(entry) if isinstance(entry, dict) else str(entry), listing)
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
        layout.addWidget(listing, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return [listing.item(index).data(Qt.UserRole) for index in range(listing.count())
                if listing.item(index).checkState() == Qt.Checked]

    def _convert(self, target):
        project = self._require_project(); paths = self._selected_inputs()
        if project and len(paths) == 1: self._start("convert", project=project, source=paths[0], target=target)

    def _repack(self):
        project = self._require_project(); rows = self.partition_table.selectionModel().selectedRows(); selection = [self.partition_table.item(i.row(), 0).text() for i in rows]
        if not project or not selection: return
        options = self._repack_options(len(selection))
        if options is None: return
        params = {"project": project, "sparse": options.pop("sparse"), "target": {"IMG": "img", "DAT": "dat", "DAT.BR": "dat.br"}[self.repack_target.currentText()], **options}
        operation = "repack" if len(selection) == 1 else "repack_batch"
        # The single-partition worker accepts one validated name; passing the
        # one-element list used by the multi-select table makes
        # ProjectLayout.validate_component raise a confusing TypeError.
        params["partition" if operation == "repack" else "partitions"] = selection[0] if operation == "repack" else selection
        self._start(operation, **params)

    def _repack_options(self, count):
        dialog = QDialog(self); dialog.setWindowTitle("回包参数"); form = QFormLayout(dialog); fs = QComboBox(); fs.addItems(["自动（按原始文件系统）", "EXT4", "EROFS"]); size = QComboBox(); size.addItems(["保留原始尺寸", "自动估算", "自定义 MiB"]); custom = QLineEdit(); comp = QComboBox(); comp.addItems(["lz4hc", "lz4", "zstd", "lzma"]); level = QSpinBox(); level.setRange(1, 12); level.setValue(9); sparse = QCheckBox("输出 Android Sparse 镜像"); form.addRow(QLabel(f"已选择 {count} 个分区")); form.addRow("文件系统", fs); form.addRow("镜像大小", size); form.addRow("自定义 MiB", custom); form.addRow("EROFS 压缩", comp); form.addRow("压缩等级", level); form.addRow(sparse); buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted: return None
        mode = {"自动（按原始文件系统）": "auto", "EXT4": "ext", "EROFS": "erofs"}[fs.currentText()]; image_size = "original" if size.currentText() == "保留原始尺寸" else "auto"
        if size.currentText() == "自定义 MiB":
            try: image_size = str(max(1, int(float(custom.text()))))
            except ValueError: QMessageBox.warning(self, "参数错误", "自定义镜像大小必须是 MiB 数字。"); return None
        return {"filesystem": mode, "image_size": image_size, "erofs_compressor": comp.currentText(), "erofs_level": level.value(), "sparse": sparse.isChecked()}

    def _repack_super(self):
        project = self._require_project(); rows = self.super_table.selectionModel().selectedRows(); sources = []
        for i in rows:
            name = self.super_table.item(i.row(), 0).text(); source = self.super_table.item(i.row(), 1).text(); sources.append(str(self.controller.root / project / source / name))
        if project and sources: self._start("repack_super", project=project, sources=sources, super_type={"A-only": 0, "A/B": 1, "Virtual A/B": 2}[self.super_type.currentText()], sparse=self.super_sparse.isChecked())

    def _thread_call(self, fn, message):
        if self.process is not None or self._thread_busy:
            return
        self._thread_busy = True
        self._busy(True); self.status.setText(message)
        def run():
            try: self.events.put({"event": "result", "data": {"outputs": fn()}})
            except Exception as e: self.events.put({"event": "error", "message": str(e)})
            self.events.put({"event": "finished"})
        threading.Thread(target=run, daemon=True).start()

    def _start(self, operation, **params):
        if self.process is not None or self._thread_busy: return
        try: self.process = self.controller.start_job(self.controller.job_request(operation, **params))
        except Exception as e: QMessageBox.critical(self, "无法启动任务", str(e)); return
        self.cancelled = False; self._busy(True); self.status.setText("任务运行中…"); self._log(f"开始任务：{operation}  ·  {params.get('project', '')}")
        process = self.process
        def read():
            for line in process.stdout:
                try: self.events.put(json.loads(line))
                except ValueError: self.events.put({"event": "log", "message": line.strip()})
            process.wait(); self.events.put({"event": "finished", "code": process.returncode})
        threading.Thread(target=read, daemon=True).start()

    def _busy(self, busy):
        for button in self._action_buttons:
            button.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy and self.process is not None); self.progress.setVisible(busy)

    def _poll(self):
        for _ in range(120):
            try: event = self.events.get_nowait()
            except queue.Empty: break
            kind = event.get("event")
            if kind == "log": self._log(event.get("message", ""))
            elif kind == "progress": self.status.setText(event.get("message", "任务运行中…"))
            elif kind == "error": self._log("失败：" + event.get("message", "")); self.status.setText("任务失败 · 详情见日志")
            elif kind == "result":
                data = event.get("data", {})
                outputs = data.get("outputs", []) if isinstance(data, dict) else []
                if isinstance(outputs, dict):
                    self._log(json.dumps(outputs, ensure_ascii=False, indent=2))
                    count = len(outputs)
                elif isinstance(outputs, (list, tuple)):
                    self._log_many(["输出：" + str(x) for x in outputs]); count = len(outputs)
                else:
                    self._log("输出：" + str(outputs)); count = 1
                self.status.setText(f"任务完成 · {count} 个结果")
            elif kind == "toolchain": self._log_many(["已下载工具：" + x for x in event.get("data", {}).get("downloaded", [])]); self.refresh_runtime()
            elif kind == "finished":
                self.process = None
                self._thread_busy = False
                self._busy(False)
                self.refresh()

    def _log(self, text): self._log_many([text])
    def _log_many(self, lines):
        clean = [ANSI.sub("", str(x)) for x in lines if x is not None]
        if clean: self.log.appendPlainText("\n".join(clean))

    def _cancel(self):
        if self.process and self.process.poll() is None:
            self.cancelled = True
            if os.name == "nt": subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **process_options())
            else: self.process.terminate()

    def _open_project(self, folder=""):
        project = self._require_project()
        if not project: return
        path = self.controller.root / project / folder
        if os.name == "nt": os.startfile(path)
        elif sys.platform == "darwin": subprocess.Popen(["open", str(path)])
        else: subprocess.Popen(["xdg-open", str(path)])

    def _browse_tools(self):
        path = QFileDialog.getExistingDirectory(self, "选择 Windows 原生工具目录")
        if path: self.tool_dir.setText(path)

    def _save_tools(self):
        try: self.controller.configure_tools(self.tool_dir.text(), self.backend.currentText()); self.refresh_runtime(); self.status.setText("工具链配置已保存")
        except Exception as e: QMessageBox.critical(self, "配置失败", str(e))

    def _bootstrap_tools(self):
        self._thread_call(lambda: self.controller.bootstrap_tools(), "正在检查并下载缺失的 Windows 工具…")

    def _copy_config(self, edit): QApplication.clipboard().setText(edit.toPlainText()); self.status.setText("MCP 连接配置已复制")

    def closeEvent(self, event):
        if self.process and self.process.poll() is None: self._cancel()
        event.accept()


def launch(root=None):
    # Ask Qt to use the Windows desktop OpenGL backend where available.  Qt's
    # backing store still falls back safely on machines without a usable GPU,
    # while normal Windows installs get DWM-composited resize/redraw.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Android ROM Toolkit for Windows")
    window = ArtWindow(ArtController(root or ROOT)); window.show(); return app.exec()


if __name__ == "__main__": launch()
