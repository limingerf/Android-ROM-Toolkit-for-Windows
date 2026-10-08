"""A.R.T desktop UI: ttkbootstrap theme, isolated jobs, live logs."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import ctypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk as standard_ttk

try:
    import ttkbootstrap as tb
except ImportError:
    tb = None

from Scripts.application import ArtController, ROOT
from Scripts.Platform.runtime import process_options

ttk = tb or standard_ttk
Window = tb.Window if tb else tk.Tk
THEME_PALETTES = {
    "light": {"bg": "#f4f7fb", "panel": "#ffffff", "sidebar": "#edf2f8",
              "border": "#d7e0eb", "text": "#1f2937", "muted": "#65758b",
              "accent": "#376fd1", "green": "#168866", "log_bg": "#f3f6fa",
              "log_fg": "#27384d", "sidebar_active": "#dce8ff", "sidebar_hover": "#e5edfb"},
    "dark": {"bg": "#101521", "panel": "#182031", "sidebar": "#121a29",
             "border": "#263249", "text": "#ecf2fc", "muted": "#93a4bc",
             "accent": "#739bff", "green": "#4fd0a2", "log_bg": "#0e1521",
             "log_fg": "#cbd9ef", "sidebar_active": "#233653", "sidebar_hover": "#223352"},
}
PALETTE = dict(THEME_PALETTES["light"])
THEME_LABELS = {"light": "浅色", "dark": "深色"}
FORMAT_NAMES = {"ext": "EXT4", "erofs": "EROFS", "sparse": "Sparse", "boot": "Boot",
                "vendor_boot": "Vendor boot", "payload": "Payload", "super": "Super",
                "dat": "DAT", "dat.br": "DAT.BR", "win": "WIN", "zip": "ROM ZIP", "unknown": "未识别"}
STATE_NAMES = {"new": "可用", "incomplete": "未初始化", "unsupported": "旧版布局", "invalid": "无效", "empty": "空工程"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def size_text(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024

class ArtApp(Window):
    def __init__(self, controller: ArtController):
        self.controller = controller
        self.ui_theme = self._load_theme()
        if os.name == "nt":
            try:
                # Set before creating Tk so Windows assigns the same identity
                # to the EXE, title bar, and taskbar button.
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "limingerf.AndroidROMToolkit.Windows2"
                )
            except (AttributeError, OSError):
                pass
        if tb:
            super().__init__(themename="flatly" if self.ui_theme == "light" else "darkly",
                             title="Android ROM Toolkit for Windows", size=(1200, 820))
        else:
            super().__init__()
        # Build the complete widget tree while hidden.  Tk otherwise exposes
        # the native black erase background before pack/grid has finished.
        self.withdraw()
        self.configure(background=PALETTE["bg"])
        self._content = tk.Frame(self, bg=PALETTE["bg"], bd=0, highlightthickness=0)
        self._content.pack(fill="both", expand=True)
        self.title("Android ROM Toolkit for Windows")
        resource_root = Path(getattr(sys, "_MEIPASS", ROOT))
        icon_paths = (resource_root / "assets" / "android-rom-toolkit.ico",
                      ROOT / "assets" / "android-rom-toolkit.ico",
                      ROOT / "art-res" / "android-rom-toolkit.ico")
        for icon_path in icon_paths:
            if icon_path.is_file():
                try:
                    self.iconbitmap(str(icon_path))
                    self.iconbitmap(default=str(icon_path))
                except tk.TclError:
                    pass
                break
        png_paths = (resource_root / "assets" / "android-rom-toolkit.png",
                     ROOT / "assets" / "android-rom-toolkit.png",
                     ROOT / "art-res" / "android-rom-toolkit.png")
        for icon_path in png_paths:
            if icon_path.is_file():
                try:
                    self._app_icon = tk.PhotoImage(file=str(icon_path))
                    self.iconphoto(True, self._app_icon)
                except tk.TclError:
                    pass
                break
        native_icon_path = next((path for path in icon_paths if path.is_file()), None)
        if os.name == "nt" and native_icon_path is not None:
            try:
                user32 = ctypes.windll.user32
                user32.LoadImageW.restype = ctypes.c_void_p
                small = user32.LoadImageW(None, str(native_icon_path), 1, 16, 16, 0x10)
                large = user32.LoadImageW(None, str(native_icon_path), 1, 32, 32, 0x10)
                if small or large:
                    self._native_icons = (small, large)
                    hwnd = self.winfo_id()
                    user32.SendMessageW(hwnd, 0x0080, 0, small or large)
                    user32.SendMessageW(hwnd, 0x0080, 1, large or small)
            except (AttributeError, OSError, tk.TclError):
                pass
        self.update_idletasks()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1320, max(1080, screen_w - 120))
        height = min(900, max(760, screen_h - 110))
        self.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 2)}")
        self.minsize(1040, 740)
        self.project_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.events = queue.Queue()
        self.process = None
        self.cancelled = False
        self.projects = []
        self.inputs = {}
        self.partitions = {}
        self.action_buttons = []
        self._log_lines = 0
        self._deferred_logs = []
        self._resize_after = None
        self._resizing = False
        self._resize_frozen = False
        self.pages = {}
        self.nav = {}
        self.current_page = "workspace"
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Configure>", self._on_configure)
        self._build_ui()
        self.update_idletasks()
        self.deiconify()
        self.after(80, self._poll)

    def _freeze_resize_paint(self, frozen):
        if os.name != "nt":
            return
        try:
            ctypes.windll.user32.SendMessageW(self.winfo_id(), 0x000B, 0 if frozen else 1, 0)
            self._resize_frozen = frozen
        except (AttributeError, OSError, tk.TclError):
            pass

    def _settings_path(self):
        path = self.controller.root / "art-res" / "ui-settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_theme(self):
        try:
            value = json.loads(self._settings_path().read_text(encoding="utf-8")).get("theme", "light")
            return value if value in THEME_PALETTES else "light"
        except (OSError, ValueError, TypeError):
            return "light"

    def _save_theme(self):
        try:
            self._settings_path().write_text(
                json.dumps({"theme": self.ui_theme}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # A read-only portable install should still be usable; the choice lasts for this session.
            pass

    def _apply_theme(self):
        PALETTE.clear()
        PALETTE.update(THEME_PALETTES[self.ui_theme])
        self.configure(bg=PALETTE["bg"])
        if hasattr(self, "_content"):
            self._content.configure(bg=PALETTE["bg"])
        if tb:
            self.style.theme_use("flatly" if self.ui_theme == "light" else "darkly")

    def _build_ui(self):
        """Build the shell and pages, allowing a theme change to rebuild cleanly."""
        self._apply_theme()
        self._configure_styles()
        self._shell()
        self._workspace_page()
        self._images_page()
        self._runtime_page()
        self._mcp_page()
        self.refresh()
        self.show_page(self.current_page)

    def _configure_styles(self):
        style = self.style if tb else standard_ttk.Style(self)
        if not tb:
            style.theme_use("clam")
            style.configure("TFrame", background=PALETTE["bg"])
            style.configure("TLabel", background=PALETTE["panel"], foreground=PALETTE["text"])
            style.configure("TButton", background=PALETTE["panel"], foreground=PALETTE["text"],
                            bordercolor=PALETTE["border"], lightcolor=PALETTE["panel"],
                            darkcolor=PALETTE["border"], padding=(13, 8), font=("Microsoft YaHei UI", 10))
            style.map("TButton", background=[("active", PALETTE["sidebar_active"]),
                                              ("disabled", PALETTE["sidebar"])],
                      foreground=[("disabled", PALETTE["muted"])])
            style.configure("TEntry", fieldbackground=PALETTE["panel"], foreground=PALETTE["text"],
                            insertcolor=PALETTE["text"], padding=6)
            style.configure("TCombobox", fieldbackground=PALETTE["panel"], foreground=PALETTE["text"], padding=5)
            style.configure("TCheckbutton", background=PALETTE["panel"], foreground=PALETTE["text"], padding=4)
            style.configure("TNotebook", background=PALETTE["panel"], bordercolor=PALETTE["border"])
            style.configure("TNotebook.Tab", background=PALETTE["sidebar"], foreground=PALETTE["text"], padding=(14, 8))
            style.map("TNotebook.Tab", background=[("selected", PALETTE["sidebar_active"])])
            style.configure("Horizontal.TProgressbar", troughcolor=PALETTE["sidebar"], background=PALETTE["accent"])
        style.configure("TFrame", background=PALETTE["panel"])
        style.configure("Treeview", background=PALETTE["panel"], foreground=PALETTE["text"],
                        fieldbackground=PALETTE["panel"])
        style.configure("Treeview.Heading", background=PALETTE["sidebar"], foreground=PALETTE["text"])
        style.configure("Treeview", rowheight=38, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        if not tb:
            style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(13, 9))
            style.configure("TCombobox", font=("Microsoft YaHei UI", 10))

    def frame(self, parent, bg=None, **kwargs):
        return tk.Frame(parent, bg=bg or PALETTE["bg"], **kwargs)

    def label(self, parent, text="", size=11, color=None, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color or PALETTE["text"],
                        font=("Microsoft YaHei UI", size, "bold" if bold else "normal"), **kwargs)

    def button(self, parent, text, command, kind="secondary", job=False):
        kwargs = {"bootstyle": kind} if tb else {}
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        if job:
            self.action_buttons.append(widget)
        return widget

    def card(self, parent, title, subtitle=""):
        frame = self.frame(parent, PALETTE["panel"], padx=22, pady=20,
                           highlightbackground=PALETTE["border"], highlightthickness=1)
        self.label(frame, title, size=13, bold=True).pack(anchor="w")
        if subtitle:
            self.label(frame, subtitle, size=9, color=PALETTE["muted"], justify="left").pack(anchor="w", pady=(5, 16))
        return frame

    def _shell(self):
        side = self.frame(self._content, PALETTE["sidebar"], width=210, padx=18, pady=26)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = self.frame(side, PALETTE["sidebar"])
        brand.pack(fill="x", pady=(0, 28))
        logo = tk.Canvas(brand, width=46, height=46, bg=PALETTE["sidebar"], highlightthickness=0)
        logo.pack(side="left")
        logo.create_rectangle(3, 3, 43, 43, fill="#3466da", outline="")
        logo.create_text(23, 23, text="A", fill="white", font=("Segoe UI", 22, "bold"))
        self.label(brand, "A.R.T", size=19, bold=True).pack(side="left", padx=10)
        self.label(side, "ANDROID ROM TOOLKIT", size=8, color=PALETTE["muted"]).pack(anchor="w", pady=(0, 24))
        for key, name in (("workspace", "工作台"), ("images", "镜像处理"), ("runtime", "工具链"), ("mcp", "MCP 连接")):
            button = tk.Button(side, text=name, anchor="w", padx=16, pady=12, relief="flat",
                               bg=PALETTE["sidebar"], fg=PALETTE["muted"], activebackground=PALETTE["sidebar_hover"],
                               activeforeground="white", font=("Microsoft YaHei UI", 11),
                               bd=0, cursor="hand2", command=lambda k=key: self.show_page(k))
            button.pack(fill="x", pady=4)
            self.nav[key] = button
        footer = self.frame(side, PALETTE["sidebar"])
        footer.pack(side="bottom", fill="x")
        self.label(footer, "Windows · Linux", size=9, color=PALETTE["muted"]).pack(anchor="w")
        self.label(footer, "桌面界面  /  CLI  /  MCP", size=8, color=PALETTE["muted"]).pack(anchor="w", pady=(6, 0))

        main = self.frame(self._content)
        main.pack(side="right", fill="both", expand=True, padx=26, pady=22)
        header = self.frame(main)
        header.pack(fill="x", pady=(0, 22))
        title = self.frame(header)
        title.pack(side="left")
        self.page_title = self.label(title, size=22, bold=True)
        self.page_title.pack(anchor="w")
        self.page_subtitle = self.label(title, size=9, color=PALETTE["muted"])
        self.page_subtitle.pack(anchor="w", pady=(5, 0))
        selector = self.frame(header)
        selector.pack(side="right", pady=8)
        self.label(selector, "当前工程", size=9, color=PALETTE["muted"]).pack(anchor="w", pady=(0, 5))
        self.project_combo = ttk.Combobox(selector, textvariable=self.project_var, width=24, state="readonly")
        self.project_combo.pack()
        self.project_combo.bind("<<ComboboxSelected>>", lambda _: self.refresh_inputs())

        self.page_host = self.frame(main)
        self.page_host.pack(fill="both", expand=True)
        footer = self.frame(main)
        footer.pack(fill="x", pady=(12, 0))
        self.status_label = self.label(footer, color=PALETTE["muted"], size=9, textvariable=self.status_var)
        self.status_label.pack(side="left")
        self.label(footer, "输入  →  工作区  →  输出", size=9, color=PALETTE["muted"]).pack(side="right")

    def page(self, key):
        frame = self.frame(self.page_host)
        self.pages[key] = frame
        return frame

    def show_page(self, key):
        self.current_page = key
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        titles = {"workspace": ("工作台", "管理 ROM 工程，保留每次构建的输入、工作区和产物"),
                  "images": ("镜像处理", "按原文件系统解包与回包，任务日志实时可见"),
                  "runtime": ("工具链", "按功能检测依赖，配置 Windows 原生工具或 WSL"),
                  "mcp": ("MCP 连接", "让支持 MCP 的客户端调用同一套工程与镜像操作")}
        self.page_title.configure(text=titles[key][0])
        self.page_subtitle.configure(text=titles[key][1])
        for name, button in self.nav.items():
            button.configure(bg=PALETTE["sidebar_active"] if name == key else PALETTE["sidebar"],
                             fg=PALETTE["text"] if name == key else PALETTE["muted"])
        if key == "images":
            self.refresh_inputs()

    def _tree(self, parent, columns):
        box = self.frame(parent, PALETTE["panel"])
        box.pack(fill="both", expand=True)
        tree = ttk.Treeview(box, columns=[item[0] for item in columns], show="headings", selectmode="extended", height=5)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=60, anchor="w", stretch=True)
        scrollbar = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    def _workspace_page(self):
        page = self.page("workspace")
        stats = self.frame(page)
        stats.pack(fill="x", pady=(0, 20))
        self.stat_vars = []
        for index, (label, detail) in enumerate((("工程", "INPUT / WORKSPACE / OUT"), ("输入文件", "原始镜像与 ROM 文件"), ("产物", "已完成的输出文件"))):
            card = self.card(stats, label)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 8, 0 if index == 2 else 8))
            stats.columnconfigure(index, weight=1)
            var = tk.StringVar(value="0")
            self.label(card, size=26, bold=True, color=PALETTE["accent"], textvariable=var).pack(anchor="w", pady=(12, 5))
            self.label(card, detail, size=9, color=PALETTE["muted"]).pack(anchor="w")
            self.stat_vars.append(var)
        area = self.frame(page)
        area.pack(fill="both", expand=True)
        projects = self.card(area, "工程列表", "双击工程进入镜像处理")
        projects.pack(side="left", fill="both", expand=True, padx=(0, 16))
        self.project_tree = self._tree(projects, (("name", "工程", 220), ("state", "状态", 80),
                                                  ("inputs", "输入", 55), ("outputs", "产物", 55)))
        self.project_tree.bind("<<TreeviewSelect>>", self._select_project)
        self.project_tree.bind("<Double-1>", lambda _: self.show_page("images"))
        row = self.frame(projects, PALETTE["panel"])
        row.pack(fill="x", pady=(14, 0))
        self.button(row, "刷新", self.refresh, "secondary-outline").pack(side="left")
        self.button(row, "打开工程目录", self._open_project, "secondary-outline").pack(side="right")

        create = self.card(area, "新建工程", "独立目录便于保留原始输入\n和每次构建的工作现场")
        create.pack(side="right", fill="y")
        self.label(create, "工程名称", size=10, color=PALETTE["muted"]).pack(anchor="w")
        self.new_name = ttk.Entry(create, width=22)
        self.new_name.pack(fill="x", pady=(8, 12))
        self.new_name.bind("<Return>", lambda _: self._create_project())
        self.button(create, "创建工程", self._create_project, "primary").pack(fill="x")
        self.label(create, "自动添加 DNA_ 前缀\n名称使用英文、数字或下划线", size=9,
                   color=PALETTE["muted"], justify="left").pack(anchor="w", pady=(16, 28))
        self.label(create, "快速开始", size=11, bold=True).pack(anchor="w")
        for step in ("01   创建或选择工程", "02   导入镜像文件", "03   提取后编辑工作区", "04   回包并检查产物"):
            self.label(create, step, size=9, color=PALETTE["muted"]).pack(anchor="w", pady=(12, 0))

    def _images_page(self):
        page = self.page("images")
        operations = self.card(page, "镜像与分区", "Payload / super 提取到 OUT；文件系统镜像提取到 WORKSPACE")
        operations.pack(fill="both", expand=True, pady=(0, 14))
        row = self.frame(operations, PALETTE["panel"])
        row.pack(fill="x", pady=(0, 14))
        self.button(row, "导入文件", self._import_files, "primary", job=True).pack(side="left")
        self.button(row, "导入 ROM ZIP", self._import_archive, "secondary-outline", job=True).pack(side="left", padx=8)
        self.button(row, "刷新", self.refresh_inputs, "secondary-outline").pack(side="left", padx=8)
        self.button(row, "打开输出", lambda: self._open_project("OUT"), "secondary-outline").pack(side="right")
        notebook = ttk.Notebook(operations)
        notebook.pack(fill="both", expand=True)
        inputs_tab = self.frame(notebook, PALETTE["panel"], padx=8, pady=8)
        partitions_tab = self.frame(notebook, PALETTE["panel"], padx=8, pady=8)
        notebook.add(inputs_tab, text="  输入文件  ")
        notebook.add(partitions_tab, text="  工作区分区  ")
        self.input_tree = self._tree(inputs_tab, (("name", "文件", 270), ("format", "格式", 100), ("size", "大小", 110)))
        actions = self.frame(inputs_tab, PALETTE["panel"])
        actions.pack(fill="x", pady=(12, 0))
        self.button(actions, "提取所选", self._extract, "primary", job=True).pack(side="left")
        self.deep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="继续解包 Payload / super 中的 IMG", variable=self.deep_var).pack(side="left", padx=12)
        self.button(actions, "转为 Sparse", lambda: self._convert("sparse"), "secondary-outline", job=True).pack(side="left", padx=8)
        self.button(actions, "转为 RAW", lambda: self._convert("raw"), "secondary-outline", job=True).pack(side="left")
        format_actions = self.frame(inputs_tab, PALETTE["panel"])
        format_actions.pack(fill="x", pady=(10, 0))
        self.label(format_actions, "按类型解包", size=9, color=PALETTE["muted"]).pack(side="left", padx=(0, 10))
        for text, fmt in (("解包 IMG", "img"), ("解包 Payload", "payload"),
                          ("解包 DAT", "dat"), ("解包 DAT.BR", "dat.br"),
                          ("解包 WIN", "win"), ("解包 super", "super")):
            self.button(format_actions, text, lambda value=fmt: self._extract_format(value),
                        "secondary-outline", job=True).pack(side="left", padx=(0, 6))
        self.partition_tree = self._tree(partitions_tab, (("name", "分区", 270), ("format", "原文件系统", 160)))
        actions = self.frame(partitions_tab, PALETTE["panel"])
        actions.pack(fill="x", pady=(12, 0))
        self.sparse_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="输出 Sparse", variable=self.sparse_var).pack(side="right", padx=10)
        self.repack_target_var = tk.StringVar(value="IMG")
        self.repack_target_combo = ttk.Combobox(actions, textvariable=self.repack_target_var,
                                                 values=("IMG", "DAT", "DAT.BR"), state="readonly", width=10)
        self.repack_target_combo.pack(side="right", padx=8)
        self.label(actions, "回包格式", size=9, color=PALETTE["muted"]).pack(side="right")
        self.button(actions, "回包所选分区", self._repack, "primary", job=True).pack(side="left")
        self.button(actions, "打开工作区", lambda: self._open_project("WORKSPACE"), "secondary-outline").pack(side="left", padx=8)

        super_tab = self.frame(notebook, PALETTE["panel"], padx=8, pady=8)
        notebook.add(super_tab, text="  合成 super  ")
        self.super_tree = self._tree(super_tab, (("name", "镜像", 280), ("source", "来源", 90), ("size", "大小", 110)))
        super_actions = self.frame(super_tab, PALETTE["panel"])
        super_actions.pack(fill="x", pady=(12, 0))
        self.super_type_var = tk.StringVar(value="A-only")
        ttk.Combobox(super_actions, textvariable=self.super_type_var,
                     values=("A-only", "A/B", "Virtual A/B"), state="readonly", width=14).pack(side="right", padx=8)
        self.super_sparse_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(super_actions, text="Sparse 输出", variable=self.super_sparse_var).pack(side="right", padx=8)
        self.button(super_actions, "合成 super.img", self._repack_super, "primary", job=True).pack(side="left")
        self.label(super_tab, "可多选 INPUT / OUT 中的分区 IMG；A/B 类型会按文件名的 _a / _b 后缀组织。", size=9,
                   color=PALETTE["muted"]).pack(anchor="w", pady=(10, 0))

        logs = self.card(page, "任务日志")
        logs.pack(fill="x")
        line = self.frame(logs, PALETTE["panel"])
        line.pack(fill="x", pady=(8, 10))
        self.progress = ttk.Progressbar(line, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_button = self.button(line, "取消", self._cancel, "danger-outline")
        self.cancel_button.pack(side="right", padx=(12, 0))
        self.cancel_button.configure(state="disabled")
        self.log = tk.Text(logs, height=7, bg=PALETTE["log_bg"], fg=PALETTE["log_fg"],
                           insertbackground=PALETTE["text"],
                           font=("Consolas", 9), relief="flat", padx=12, pady=10, state="disabled", wrap="word")
        self.log.pack(fill="x")
        self._log("就绪。选择工程并导入文件后开始处理。")

    def _runtime_page(self):
        page = self.page("runtime")
        appearance = self.card(page, "界面外观", "选择适合当前环境的界面主题；设置会自动保存")
        appearance.pack(fill="x", pady=(0, 14))
        appearance_row = self.frame(appearance, PALETTE["panel"])
        appearance_row.pack(fill="x")
        self.label(appearance_row, "主题", size=10, color=PALETTE["muted"]).pack(side="left", padx=(0, 12))
        self.theme_var = tk.StringVar(value=THEME_LABELS[self.ui_theme])
        self.theme_combo = ttk.Combobox(appearance_row, textvariable=self.theme_var,
                                         values=("浅色", "深色"), state="readonly", width=12)
        self.theme_combo.pack(side="left")
        self.theme_combo.bind("<<ComboboxSelected>>", self._change_theme)
        config = self.card(page, "运行后端", "Windows 原生无需 WSL；个别缺失的工具只影响对应功能")
        config.pack(fill="x", pady=(0, 18))
        row = self.frame(config, PALETTE["panel"])
        row.pack(fill="x")
        self.backend_var = tk.StringVar(value="native")
        self.backend_combo = ttk.Combobox(row, textvariable=self.backend_var, values=("native", "wsl"), state="readonly", width=12)
        self.backend_combo.pack(side="left", padx=(0, 12))
        self.directory_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.directory_var).pack(side="left", fill="x", expand=True)
        self.button(row, "浏览", self._browse_tools, "secondary-outline").pack(side="left", padx=8)
        self.button(row, "保存配置", self._save_tools, "primary").pack(side="left")
        self.backend_label = self.label(config, color=PALETTE["muted"], size=9)
        self.backend_label.pack(anchor="w", pady=(12, 0))
        availability = self.card(page, "功能可用性", "可用状态依据工具文件检测；实际命令执行结果以任务日志为准")
        availability.pack(fill="both", expand=True)
        self.capability_tree = self._tree(availability, (("name", "功能", 180), ("status", "状态", 90), ("tools", "工具", 380)))
        self.button(config, "自动补齐 Windows 工具", self._bootstrap_tools, "secondary-outline").pack(anchor="e", pady=(12, 0))
        self.button(availability, "重新检测", self.refresh_runtime, "secondary-outline").pack(anchor="e", pady=(16, 0))

    def _mcp_page(self):
        page = self.page("mcp")
        card = self.card(page, "连接你的 AI 客户端", "复制配置到支持 MCP stdio 的客户端，路径按当前安装位置生成")
        card.pack(fill="both", expand=True)
        command = [sys.executable, "--mcp", "--root", str(self.controller.root)] if getattr(sys, "frozen", False) else [sys.executable, str(ROOT / "Scripts" / "main.py"), "--mcp", "--root", str(self.controller.root)]
        self.mcp_config = json.dumps({"mcpServers": {"art": {"command": command[0], "args": command[1:]}}}, ensure_ascii=False, indent=2)
        text = tk.Text(card, height=13, bg=PALETTE["log_bg"], fg=PALETTE["log_fg"], font=("Consolas", 10), relief="flat",
                       padx=16, pady=16, wrap="word")
        text.pack(fill="x", pady=(0, 16))
        text.insert("1.0", self.mcp_config)
        text.configure(state="disabled")
        self.button(card, "复制连接配置", self._copy_config, "primary").pack(anchor="w")
        self.label(card, "可调用工具", size=12, bold=True).pack(anchor="w", pady=(28, 12))
        for value in ("工程创建 / 列表 · 输入识别 / 导入", "镜像提取 · 分区回包 · RAW / Sparse 转换", "工具链诊断 · 工作区分区列表"):
            self.label(card, value, size=10, color=PALETTE["muted"]).pack(anchor="w", pady=5)

    def refresh(self):
        self.projects = self.controller.list_projects()
        for iid in self.project_tree.get_children():
            self.project_tree.delete(iid)
        for item in self.projects:
            self.project_tree.insert("", "end", iid=item["name"], values=(item["name"], STATE_NAMES.get(item["state"], item["state"]),
                                                                         item["input_count"], item["output_count"]))
        names = [p["name"] for p in self.projects if p["state"] == "new"]
        self.project_combo.configure(values=names)
        if self.project_var.get() not in names:
            self.project_var.set(names[0] if names else "")
        for var, value in zip(self.stat_vars, (len(self.projects), sum(p["input_count"] for p in self.projects),
                                               sum(p["output_count"] for p in self.projects))):
            var.set(str(value))
        self.refresh_inputs()
        self.refresh_runtime()

    def refresh_inputs(self):
        for tree in (self.input_tree, self.partition_tree):
            for iid in tree.get_children():
                tree.delete(iid)
        self.inputs, self.partitions = {}, {}
        if hasattr(self, "output_tree"):
            for iid in self.output_tree.get_children():
                self.output_tree.delete(iid)
        if hasattr(self, "super_tree"):
            for iid in self.super_tree.get_children():
                self.super_tree.delete(iid)
        if not self.project_var.get():
            return
        try:
            for index, item in enumerate(self.controller.list_inputs(self.project_var.get())):
                key = str(index)
                self.inputs[key] = item
                self.input_tree.insert("", "end", iid=key, values=(item["name"], FORMAT_NAMES.get(item["format"], item["format"]), size_text(item["size"])))
            for item in self.controller.list_partitions(self.project_var.get()):
                self.partitions[item["name"]] = item
                self.partition_tree.insert("", "end", iid=item["name"], values=(item["name"], FORMAT_NAMES.get(item["format"], item["format"])))
            for index, item in enumerate(self.controller.list_outputs(self.project_var.get())):
                key = f"out:{index}"
                if hasattr(self, "super_tree") and item["name"].lower().endswith(".img"):
                    self.super_tree.insert("", "end", iid=key, values=(item["name"], "OUT", size_text(item["size"])))
            if hasattr(self, "super_tree"):
                for index, item in enumerate(self.inputs.values()):
                    if item["name"].lower().endswith(".img"):
                        key = f"in:{index}"
                        self.super_tree.insert("", "end", iid=key, values=(item["name"], "INPUT", size_text(item["size"])))
        except Exception as error:
            self.status_var.set(str(error))

    def refresh_runtime(self):
        from Scripts.Platform.runtime import CAPABILITIES
        status = self.controller.toolchain_status()
        self.backend_var.set("wsl" if status["mode"] == "wsl" else "native")
        config = ROOT / "art-res" / "host-tools.local.json"
        local = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        self.directory_var.set(os.environ.get("ART_WINDOWS_TOOLS") or local.get("windows_tools", ""))
        self.backend_label.configure(text=f"{status['label']}  ·  {len(status['tools']) - len(status['missing'])}/{len(status['tools'])} 个外部工具已找到")
        for iid in self.capability_tree.get_children():
            self.capability_tree.delete(iid)
        for name, ready in status["capabilities"].items():
            tools = CAPABILITIES[name]
            self.capability_tree.insert("", "end", values=(name, "可用" if ready else "缺少工具", " / ".join(tools) or "内置 Python 实现"))

    def _select_project(self, _event):
        selection = self.project_tree.selection()
        if selection:
            self.project_var.set(selection[0])
            self.refresh_inputs()

    def _import_archive(self):
        project = self._require_project()
        if not project:
            return
        path = filedialog.askopenfilename(title="选择 ROM ZIP", filetypes=(("ROM ZIP", "*.zip"), ("所有文件", "*.*")), parent=self)
        if not path:
            return
        self._busy(True)
        self.status_var.set("正在导入 ROM ZIP…")
        def task():
            try:
                outputs = self.controller.import_rom_archive(project, path)
                self.events.put({"event": "result", "data": {"outputs": outputs}})
            except Exception as error:
                self.events.put({"event": "error", "message": str(error)})
            self.events.put({"event": "finished"})
        threading.Thread(target=task, daemon=True).start()

    def _change_theme(self, _event=None):
        mode = "light" if self.theme_var.get() == "浅色" else "dark"
        if mode == self.ui_theme:
            return
        self.ui_theme = mode
        self._save_theme()
        # Rebuild the small shell so every custom Tk frame, label and text area gets
        # the new palette as well as ttkbootstrap's native widgets.
        for child in self.winfo_children():
            child.destroy()
        self._content = tk.Frame(self, bg=PALETTE["bg"], bd=0, highlightthickness=0)
        self._content.pack(fill="both", expand=True)
        self.pages = {}
        self.nav = {}
        self.action_buttons = []
        self._log_lines = 0
        self._build_ui()

    def _create_project(self):
        try:
            result = self.controller.create_project(self.new_name.get())
            self.project_var.set(result["name"])
            self.new_name.delete(0, "end")
            self.refresh()
            self.status_var.set("工程已创建：" + result["name"])
        except Exception as error:
            messagebox.showerror("创建工程失败", str(error), parent=self)

    def _require_project(self):
        if not self.project_var.get():
            messagebox.showinfo("选择工程", "请先创建或选择工程。", parent=self)
            return None
        return self.project_var.get()

    def _import_files(self):
        project = self._require_project()
        if not project:
            return
        paths = filedialog.askopenfilenames(title="导入镜像及 transfer.list", parent=self)
        if not paths:
            return
        self._busy(True)
        self.status_var.set("正在导入文件…")
        def task():
            try:
                result = self.controller.import_inputs(project, list(paths))
                self.events.put({"event": "result", "data": {"outputs": result}})
            except Exception as error:
                self.events.put({"event": "error", "message": str(error)})
            self.events.put({"event": "finished"})
        threading.Thread(target=task, daemon=True).start()

    def _extract(self):
        project = self._require_project()
        if not project:
            return
        selected = [self.inputs[key]["path"] for key in self.input_tree.selection()]
        if not selected:
            messagebox.showinfo("选择输入", "请在输入文件列表中选择需要提取的文件。", parent=self)
            return
        self._start("extract", project=project, sources=selected, deep=self.deep_var.get())

    def _extract_format(self, requested):
        """Expose the original A.R.T per-format extraction choices in the GUI."""
        project = self._require_project()
        if not project:
            return
        type_map = {
            "img": {"ext", "erofs", "sparse", "boot", "vendor_boot"},
            "payload": {"payload"}, "dat": {"dat"}, "dat.br": {"dat.br"},
            "win": {"win"}, "super": {"super"},
        }
        formats = type_map[requested]
        selected = [self.inputs[key] for key in self.input_tree.selection()]
        matches = [item for item in selected if item["format"] in formats]
        if not matches:
            matches = [item for item in self.inputs.values() if item["format"] in formats]
        if not matches:
            title = "解包 IMG" if requested == "img" else f"解包 {requested.upper()}"
            messagebox.showinfo(title, f"输入文件中没有可用的 {title[3:]} 文件。", parent=self)
            return
        self._start("extract", project=project,
                    sources=[item["path"] for item in matches], deep=self.deep_var.get())

    def _convert(self, target):
        project = self._require_project()
        selection = self.input_tree.selection()
        if not project:
            return
        if len(selection) != 1:
            messagebox.showinfo("选择镜像", "请只选择一个镜像进行转换。", parent=self)
            return
        self._start("convert", project=project, source=self.inputs[selection[0]]["path"], target=target)

    def _repack(self):
        project = self._require_project()
        selection = self.partition_tree.selection()
        if not project:
            return
        if not selection:
            messagebox.showinfo("选择分区", "请选择至少一个工作区分区进行回包。", parent=self)
            return
        options = self._repack_options(selection)
        if options is None:
            return
        target = {"IMG": "img", "DAT": "dat", "DAT.BR": "dat.br"}[self.repack_target_var.get()]
        operation = "repack" if len(selection) == 1 else "repack_batch"
        params = {"project": project, "sparse": options["sparse"], "target": target, **options}
        if operation == "repack":
            params["partition"] = selection[0]
        else:
            params["partitions"] = list(selection)
        self._start(operation, **params)

    def _repack_options(self, selection):
        dialog = tk.Toplevel(self)
        dialog.title("回包参数")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.configure(bg=PALETTE["panel"])
        dialog.update_idletasks()
        parent_x, parent_y = self.winfo_rootx(), self.winfo_rooty()
        parent_w, parent_h = self.winfo_width(), self.winfo_height()
        dialog_w, dialog_h = 540, 520
        dialog.geometry(f"{dialog_w}x{dialog_h}+{max(0, parent_x + (parent_w - dialog_w) // 2)}+{max(0, parent_y + (parent_h - dialog_h) // 2)}")
        dialog.grab_set()
        result = {}
        pad = {"padx": 14, "pady": 7}
        body = tk.Frame(dialog, bg=PALETTE["panel"], padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=f"已选择 {len(selection)} 个分区", bg=PALETTE["panel"], fg=PALETTE["text"],
                 font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(body, text="文件系统", bg=PALETTE["panel"], fg=PALETTE["muted"]).grid(row=1, column=0, sticky="w", **pad)
        filesystem = tk.StringVar(value="自动（按原始文件系统）")
        ttk.Combobox(body, textvariable=filesystem, state="readonly", width=27,
                     values=("自动（按原始文件系统）", "EXT4", "EROFS")).grid(row=1, column=1, sticky="ew", **pad)
        tk.Label(body, text="镜像大小", bg=PALETTE["panel"], fg=PALETTE["muted"]).grid(row=2, column=0, sticky="w", **pad)
        size_mode = tk.StringVar(value="自动估算")
        ttk.Combobox(body, textvariable=size_mode, state="readonly", width=27,
                     values=("保留原始尺寸", "自动估算", "自定义 MiB")).grid(row=2, column=1, sticky="ew", **pad)
        tk.Label(body, text="自定义 MiB", bg=PALETTE["panel"], fg=PALETTE["muted"]).grid(row=3, column=0, sticky="w", **pad)
        custom_size = tk.StringVar(value="")
        ttk.Entry(body, textvariable=custom_size, width=29).grid(row=3, column=1, sticky="ew", **pad)
        tk.Label(body, text="EROFS 压缩", bg=PALETTE["panel"], fg=PALETTE["muted"]).grid(row=4, column=0, sticky="w", **pad)
        compressor = tk.StringVar(value="lz4hc")
        ttk.Combobox(body, textvariable=compressor, state="readonly", width=27,
                     values=("lz4hc", "lz4", "zstd", "lzma")).grid(row=4, column=1, sticky="ew", **pad)
        tk.Label(body, text="压缩等级", bg=PALETTE["panel"], fg=PALETTE["muted"]).grid(row=5, column=0, sticky="w", **pad)
        level = tk.StringVar(value="9")
        ttk.Combobox(body, textvariable=level, state="readonly", width=27,
                     values=tuple(str(i) for i in range(1, 13))).grid(row=5, column=1, sticky="ew", **pad)
        sparse = tk.BooleanVar(value=self.sparse_var.get())
        ttk.Checkbutton(body, text="输出 Android Sparse 镜像", variable=sparse).grid(row=6, column=0, columnspan=2, sticky="w", **pad)
        hint = tk.Label(body, text="vendor/odm 会保留原始 EXT4 几何结构；修改后仍需原位替换流程。",
                        bg=PALETTE["panel"], fg=PALETTE["muted"], wraplength=420, justify="left")
        hint.grid(row=7, column=0, columnspan=2, sticky="w", **pad)
        buttons = tk.Frame(body, bg=PALETTE["panel"])
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        def accept():
            selected_fs = {"自动（按原始文件系统）": "auto", "EXT4": "ext", "EROFS": "erofs"}[filesystem.get()]
            selected_size = "auto"
            if size_mode.get() == "保留原始尺寸":
                selected_size = "original"
            elif size_mode.get() == "自定义 MiB":
                try:
                    selected_size = str(max(1, int(float(custom_size.get()))))
                except ValueError:
                    messagebox.showerror("参数错误", "自定义镜像大小必须是 MiB 数字。", parent=dialog)
                    return
            result.update(filesystem=selected_fs, image_size=selected_size,
                          erofs_compressor=compressor.get(), erofs_level=int(level.get()), sparse=sparse.get())
            dialog.destroy()
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="开始回包", command=accept).pack(side="right")
        self.wait_window(dialog)
        return result or None

    def _repack_super(self):
        project = self._require_project()
        if not project:
            return
        selection = self.super_tree.selection()
        if not selection:
            messagebox.showinfo("选择镜像", "请在合成 super 页面选择至少一个 IMG。", parent=self)
            return
        sources = []
        for iid in selection:
            name, source, _size = self.super_tree.item(iid, "values")
            sources.append(str(self.controller.root / project / source / name))
        super_type = {"A-only": 0, "A/B": 1, "Virtual A/B": 2}[self.super_type_var.get()]
        self._start("repack_super", project=project, sources=sources,
                    super_type=super_type, sparse=self.super_sparse_var.get())

    def _start(self, operation, **params):
        if self.process is not None:
            return
        try:
            request = self.controller.job_request(operation, **params)
            self.process = self.controller.start_job(request)
        except Exception as error:
            messagebox.showerror("无法启动任务", str(error), parent=self)
            return
        self.cancelled = False
        self._busy(True)
        self.cancel_button.configure(state="normal")
        self.status_var.set("任务运行中…")
        self._log("─" * 46)
        self._log(f"开始任务：{operation}  ·  {params['project']}")
        process = self.process
        def read():
            for line in process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    self.events.put({"event": "log", "message": line.strip()})
            process.wait()
            self.events.put({"event": "finished", "code": process.returncode})
        threading.Thread(target=read, daemon=True).start()

    def _busy(self, value):
        for button in self.action_buttons:
            button.configure(state="disabled" if value else "normal")
        self.project_combo.configure(state="disabled" if value else "readonly")
        if value:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.cancel_button.configure(state="disabled")

    def _poll(self):
        pending_logs = self._deferred_logs

        def flush_logs():
            if pending_logs and not self._resizing:
                self._log_many(pending_logs[:])
                pending_logs.clear()

        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("event")
            if kind == "log":
                pending_logs.append(event["message"])
            elif kind == "progress":
                flush_logs()
                self.status_var.set(event.get("message", "任务运行中…"))
            elif kind == "error":
                pending_logs.append("失败：" + event["message"])
                flush_logs()
                self.status_var.set("任务失败 · 详情见日志")
            elif kind == "result":
                flush_logs()
                outputs = event.get("data", {}).get("outputs", [])
                self._log_many(["输出：" + path for path in outputs])
                self.status_var.set(f"任务完成 · {len(outputs)} 个产物")
            elif kind == "toolchain":
                flush_logs()
                data = event.get("data", {})
                self._log_many(["已下载工具：" + name for name in data.get("downloaded", [])])
                self._log_many(["工具下载失败：" + error for error in data.get("errors", [])])
                self.status_var.set("工具链检查完成")
                self.refresh_runtime()
            elif kind == "finished":
                flush_logs()
                self.process = None
                self._busy(False)
                if self.cancelled:
                    self._log("任务已取消；未发布的中间文件可能保留在 .art-job-*。")
                    self.status_var.set("任务已取消")
                self.refresh()
        flush_logs()
        self._deferred_logs = pending_logs
        self.after(80, self._poll)

    def _on_configure(self, event):
        """Debounce work that competes with Tk's resize redraws.

        Windows sends many Configure events during a drag.  Pausing the
        progress animation and log widget mutations for that short interval
        keeps the window responsive while the geometry manager is recalculating
        the cards and Treeviews.
        """
        if event.widget is not self:
            return
        self._resizing = True
        if not self._resize_frozen:
            self._freeze_resize_paint(True)
        if self.process is not None:
            self.progress.stop()
        if self._resize_after is not None:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(180, self._finish_resize)

    def _finish_resize(self):
        self._resize_after = None
        self._resizing = False
        if self._resize_frozen:
            self._freeze_resize_paint(False)
        if self._deferred_logs:
            self._log_many(self._deferred_logs[:])
            self._deferred_logs.clear()
        if self.process is not None and not self.cancelled:
            self.progress.start(12)
        if os.name == "nt":
            try:
                ctypes.windll.user32.RedrawWindow(self.winfo_id(), None, None, 0x0001 | 0x0004 | 0x0080)
            except (AttributeError, OSError, tk.TclError):
                pass


    def _log(self, message):
        self._log_many([message])

    def _log_many(self, messages):
        messages = [ANSI.sub("", str(message)) for message in messages if message is not None]
        if not messages:
            return
        self.log.configure(state="normal")
        self.log.insert("end", "\n".join(messages) + "\n")
        self._log_lines += len(messages)
        # Keep the text widget bounded.  Large EROFS/EXT4 jobs can produce
        # many thousands of tool lines, which otherwise makes Tk increasingly
        # expensive to redraw and is the main source of UI stutter.
        if self._log_lines > 20000:
            self.log.delete("1.0", "10001.0")
            self._log_lines -= 10000
        self.log.see("end")
        self.log.configure(state="disabled")

    def _cancel(self):
        if self.process and self.process.poll() is None:
            self.cancelled = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **process_options())
            else:
                self.process.terminate()

    def _close(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("关闭窗口", "任务仍在运行。取消任务并关闭窗口？", parent=self):
                return
            self._cancel()
        self.destroy()

    def _open_project(self, folder=""):
        project = self._require_project()
        if not project:
            return
        path = self.controller.root / project / folder
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _browse_tools(self):
        directory = filedialog.askdirectory(title="选择 Windows 原生工具目录", parent=self)
        if directory:
            self.directory_var.set(directory)

    def _save_tools(self):
        try:
            self.controller.configure_tools(self.directory_var.get(), self.backend_var.get())
            self.refresh_runtime()
            self.status_var.set("工具链配置已保存")
        except Exception as error:
            messagebox.showerror("配置失败", str(error), parent=self)

    def _bootstrap_tools(self):
        self._busy(True)
        self.status_var.set("正在检查并下载缺失的 Windows 工具…")
        def task():
            try:
                result = self.controller.bootstrap_tools()
                self.events.put({"event": "toolchain", "data": result})
            except Exception as error:
                self.events.put({"event": "error", "message": str(error)})
            self.events.put({"event": "finished"})
        threading.Thread(target=task, daemon=True).start()

    def _copy_config(self):
        self.clipboard_clear()
        self.clipboard_append(self.mcp_config)
        self.status_var.set("MCP 连接配置已复制")

def launch(root=None):
    ArtApp(ArtController(root or ROOT)).mainloop()

if __name__ == "__main__":
    launch()
