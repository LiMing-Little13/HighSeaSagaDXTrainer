import ctypes
import queue
import re
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, ttk

from app_settings import (
    DEFAULT_BACKUP_ROOT, DEFAULT_LOCK_INTERVAL_MS, SettingsStore, validate_backup_root,
)
from domain import (
    PARAM_NAMES, PARAM_ONECLICK_BASE, PARAM_ONECLICK_GROWTH,
    PARAM_SAFE_LIMITED, PARAM_SAFE_TOTAL, PARAM_SUM_LIMIT,
    RESOURCE_FIELDS, RESOURCE_INPUT_MAX, RESOURCE_SAFE_MAX,
)

from game_backend import GAME_NAME, IL2CPPGame


class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        widget.bind("<FocusIn>", self.show)
        widget.bind("<FocusOut>", self.hide)

    def show(self, _event=None):
        if self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_attributes("-topmost", True)
        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.window,
            text=self.text,
            justify="left",
            wraplength=420,
            background="#fff8dc",
            relief="solid",
            borderwidth=1,
            padx=9,
            pady=7,
        ).pack()

    def hide(self, _event=None):
        if self.window is not None:
            self.window.destroy()
            self.window = None


class GameWorker:
    def __init__(self, backup_root):
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.backup_root = backup_root
        self.thread = threading.Thread(target=self._run, name="game-worker", daemon=True)
        self.thread.start()

    def _run(self):
        game = None
        while True:
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                job = ()
            if job is None:
                lock_error = None
                if game is not None:
                    try:
                        game.clear_step_lock()
                    except Exception as exc:
                        lock_error = str(exc)
                try:
                    if game is not None:
                        game.close()
                except Exception as exc:
                    message = str(exc)
                    if lock_error:
                        message += f"\n步数锁定清理也失败：{lock_error}"
                    self.results.put(("close_error", message))
                    continue
                self.results.put(("closed", None))
                return
            if job:
                token, operation = job
                try:
                    if game is None:
                        game = IL2CPPGame(backup_root=self.backup_root)
                    result = operation(game)
                except Exception as exc:
                    self.results.put(("result", (token, None, exc)))
                else:
                    self.results.put(("result", (token, result, None)))
            if game is None:
                continue
            try:
                for event in game.drain_events():
                    self.results.put(("event", event))
            except Exception as exc:
                self.results.put(("event", {"type": "error", "message": str(exc)}))


class ScrollPage(ttk.Frame):
    def __init__(self, parent, padding=10):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f5f6f8")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.content = ttk.Frame(self.canvas, padding=padding)
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._content_size)
        self.canvas.bind("<Configure>", self._canvas_size)

    def _content_size(self, _event=None):
        height = max(self.content.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.itemconfigure(self.window, height=height)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_size(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)
        self._content_size()

    def wheel(self, event):
        if self.content.winfo_reqheight() <= self.canvas.winfo_height():
            return None
        delta = -int(event.delta / 120) if event.delta else (1 if event.num == 5 else -1)
        self.canvas.yview_scroll(delta, "units")
        return "break"


class TrainerApp(tk.Tk):
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        super().__init__()
        dpi_scale = self.winfo_fpixels("1i") / 96
        self.title("High Sea Saga DX 直接属性修改器")
        self.geometry(f"{round(760 * dpi_scale)}x{round(680 * dpi_scale)}")
        self.minsize(640, 480)
        self.settings_store = SettingsStore()
        self._settings_error = None
        self.settings = self.load_settings()
        self._closing = False
        self._close_failed = False
        self._busy = None
        self._job_serial = 0
        self._callbacks = {}
        self._connected = False
        self._geometry_timer_id = None
        self._geometry_enable_timer_id = None
        self._auto_connect_timer_id = None
        self._normal_geometry = f"{round(760 * dpi_scale)}x{round(680 * dpi_scale)}+0+0"
        self._scroll_pages = []
        self.worker = GameWorker(self.settings.get("backup_root", str(DEFAULT_BACKUP_ROOT)))
        self.snapshot = None
        self.crew_position = 0  # 下拉框位置，不是游戏内 crew.index
        self.process_var = tk.StringVar(value="未连接")
        self.status_var = tk.StringVar(value="正在准备连接游戏……")
        self.summary_var = tk.StringVar(value="尚未读取数据")
        self.resource_vars = {name: tk.StringVar() for name, _label in RESOURCE_FIELDS}
        self.resource_warn_var = tk.StringVar()
        self.crew_choice_var = tk.StringVar()
        self.crew_vars = {
            name: tk.StringVar()
            for name in ("level", "exp", "battleHP", "battleSP")
        }
        self.param_vars = [tk.StringVar() for _ in PARAM_NAMES]
        self.add_param_vars = [tk.StringVar() for _ in PARAM_NAMES]
        self.crew_headroom_var = tk.StringVar(value="请先刷新读取数据")
        self.item_tab_names = ("武器", "道具", "强化")
        self.item_tabs = {
            tab: {
                "checked": set(),
                "value_var": tk.StringVar(),
                "limit_var": tk.StringVar(value="请勾选要修改的物品"),
            }
            for tab in range(len(self.item_tab_names))
        }
        self.checked_cards = set()
        self.card_value_var = tk.StringVar()
        self.card_limit_var = tk.StringVar(value="请勾选要修改的卡片")
        self.dice_count_var = tk.StringVar(value="1")
        self.dice_step_var = tk.StringVar()
        self.dice_remaining_var = tk.StringVar()
        self.board_info_var = tk.StringVar(value="尚未读取棋盘状态，请刷新")
        self.goto_var = tk.StringVar(value="1")
        self.goto_status_var = tk.StringVar(value="设置后，下一次投掷会按实时位置自动计算步数走过去")
        self.kairo_point_var = tk.StringVar()
        self.kairo_point_limit_var = tk.StringVar(value="请先刷新读取数据")
        self.lock_remaining_var = tk.BooleanVar(value=False)
        self.lock_step_var = tk.BooleanVar(value=False)
        self._lock_timer_id = None
        self._lock_target_value = 0
        self._lock_failures = 0
        self.resource_lock_vars = {name: tk.BooleanVar(value=False) for name, _label in RESOURCE_FIELDS}
        self._resource_lock_targets = {}
        self._resource_lock_timer_id = None
        self._resource_lock_failures = 0
        self.resource_entries = {}
        self._resource_labels = dict(RESOURCE_FIELDS)
        self._column_save_timer_id = None
        self.backup_root_var = tk.StringVar(value=self.settings.get("backup_root", str(DEFAULT_BACKUP_ROOT)))
        self.backup_hint_var = tk.StringVar(value=f"存档备份目录：{self.backup_root_var.get()}")
        self.lock_interval_var = tk.StringVar(value=f"{self.settings.get('lock_interval_ms', DEFAULT_LOCK_INTERVAL_MS) / 1000:g}")
        self.auto_connect_var = tk.BooleanVar(value=self.settings.get("auto_connect", True))
        self.tooltips = []
        self._setup_style()
        self._build_ui()
        self._restore_window()
        self.bind("<Configure>", self._window_changed, add="+")
        self.bind_all("<MouseWheel>", self._route_wheel, add="+")
        self.bind_all("<Button-4>", self._route_wheel, add="+")
        self.bind_all("<Button-5>", self._route_wheel, add="+")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self._poll_worker)
        if self._settings_error:
            self.status_var.set(f"设置读取失败，使用默认值：{self._settings_error}")
        elif self.auto_connect_var.get():
            self._auto_connect_timer_id = self.after(100, self.connect)
        else:
            self.status_var.set("请启动游戏并载入存档后连接")

    def _submit(self, title, operation, success=None, failure=None, quiet=False, require_connection=True, background=False):
        if self._closing or self._close_failed:
            return False
        if self._busy is not None:
            if not quiet:
                self.status_var.set(f"正在{self._busy}，请等待完成")
            return False
        if require_connection and not self._connected:
            if not quiet:
                self.status_var.set("请先连接游戏")
            return False
        self._job_serial += 1
        # 后台轮询不占用前台“忙碌”标记：否则每 700ms 一次的锁定检查会挡住用户点击应用。
        if not background:
            self._busy = title
        self._callbacks[self._job_serial] = (title, success, failure, quiet, background)
        self.worker.jobs.put((self._job_serial, operation))
        if not background:
            self._set_controls_busy(True)
        if not quiet:
            self.status_var.set(f"正在{title}……")
        return True

    def _set_controls_busy(self, busy):
        def visit(widget):
            if isinstance(widget, (ttk.Button, ttk.Checkbutton, ttk.Combobox, ttk.Entry, ttk.Spinbox)):
                if busy:
                    widget.state(["disabled"])
                else:
                    widget.state(["!disabled"])
            for child in widget.winfo_children():
                visit(child)
        visit(self.notebook)

    def _poll_worker(self):
        while True:
            try:
                kind, payload = self.worker.results.get_nowait()
            except queue.Empty:
                break
            if kind == "closed":
                self.destroy()
                return
            if kind == "close_error":
                self._close_failed = True
                self._closing = False
                self._stop_lock_remaining()
                self.lock_remaining_var.set(False)
                self._stop_resource_lock()
                self._set_controls_busy(True)
                self.status_var.set("关闭失败：后端引用已保留，已停止次数锁定；请再次关闭窗口以重试")
                messagebox.showerror("关闭失败", f"{payload}\n\n尚未确认释放游戏连接，已阻止新连接和写入。\n请再次关闭窗口以重试。", parent=self)
                continue
            if kind == "event":
                if self._closing or self._close_failed:
                    continue
                if payload.get("type") == "detached":
                    self._reset_connection()
                    self.status_var.set(f"游戏连接已断开：{payload.get('message', '')}")
                elif payload.get("type") in ("error", "message"):
                    self.status_var.set(payload.get("message", ""))
                continue
            token, value, error = payload
            title, success, failure, quiet, background = self._callbacks.pop(token)
            if not background:
                self._busy = None
            if self._closing:
                continue
            if not background:
                self._set_controls_busy(False)
            if error is not None:
                if failure:
                    failure(error)
                if not quiet:
                    self.status_var.set(f"{title}失败")
                    messagebox.showerror(f"{title}失败", str(error), parent=self)
            elif success:
                success(value)
        self.after(50, self._poll_worker)

    def _reset_connection(self):
        self._connected = False
        self._stop_lock_remaining()
        self.lock_remaining_var.set(False)
        self.lock_step_var.set(False)
        self._lock_target_value = 0
        self._stop_resource_lock()
        self._resource_lock_targets.clear()
        for var in self.resource_lock_vars.values():
            var.set(False)
        self.snapshot = None
        self.process_var.set("未连接")
        self.summary_var.set("尚未读取数据")
        for var in self.resource_vars.values():
            var.set("")
        self.resource_warn_var.set("")
        for var in list(self.crew_vars.values()) + self.param_vars + self.add_param_vars:
            var.set("")
        self.crew_selector.configure(values=())
        self.crew_choice_var.set("")
        for info in self.item_tabs.values():
            info["checked"].clear()
            info["tree"].delete(*info["tree"].get_children())
            info["value_var"].set("")
            info["limit_var"].set("请先连接游戏")
        self.checked_cards.clear()
        self.card_tree.delete(*self.card_tree.get_children())
        self.card_value_var.set("")
        self.card_limit_var.set("请先连接游戏")
        self.dice_remaining_var.set("")
        self.dice_step_var.set("")
        self.kairo_point_var.set("")
        self.board_info_var.set("尚未读取棋盘状态，请刷新")
        self.goto_status_var.set("尚未设置目标格")

    def disconnect(self):
        if self._busy is not None or self._closing:
            if self._busy is not None:
                self.status_var.set(f"正在{self._busy}，请等待完成")
            return
        self._stop_lock_remaining()
        self._submit("断开游戏", lambda game: game.close(), lambda _value: self._disconnected(), require_connection=False)

    def _disconnected(self):
        self._reset_connection()
        self.status_var.set("已断开游戏")

    def _submit_write(self, title, *, write, read_back, apply_readback, message, on_written=None):
        def run(game):
            write(game)
            try:
                return read_back(game), None
            except Exception as exc:
                return None, exc

        def done(payload):
            data, read_error = payload
            if on_written is not None:
                on_written()
            if read_error is not None:
                self.status_var.set(f"{title}已写入，范围回读失败：{read_error}；请手动刷新确认")
                messagebox.showwarning("写入已完成", f"写入已完成，但回读失败：{read_error}\n请勿重复提交，请先刷新确认。", parent=self)
                return
            apply_readback(data)
            self.status_var.set(message)

        return self._submit(title, run, done)

    def _scroll_page(self, notebook, title, padding=10):
        page = ScrollPage(notebook, padding=padding)
        notebook.add(page, text=title)
        self._scroll_pages.append(page)
        return page.content

    def _route_wheel(self, event):
        widget = event.widget
        current = widget
        while current is not None:
            if isinstance(current, (ttk.Treeview, tk.Text, tk.Listbox, ttk.Combobox, ttk.Spinbox)):
                return None
            if isinstance(current, ScrollPage):
                return current.wheel(event)
            current = getattr(current, "master", None)
        return None

    def _wrap(self, label):
        label.configure(justify="left", anchor="w")
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(80, event.width - 4)), add="+")
        return label

    def add_help(self, parent, text):
        label = ttk.Label(parent, text="？", foreground="#1769aa", cursor="question_arrow", takefocus=1)
        self.tooltips.append(Tooltip(label, text))
        return label

    def _setup_style(self):
        available = set(tkfont.families())
        family = next(
            (name for name in ("LXGW WenKai", "霞鹜文楷", "Microsoft YaHei UI") if name in available),
            "TkDefaultFont",
        )
        self.font_base = tkfont.Font(family=family, size=9)
        self.font_heading = tkfont.Font(family=family, size=10, weight="bold")
        self.font_big = tkfont.Font(family=family, size=14)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=9)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=self.font_base)
        style.configure("TFrame", background="#f5f6f8")
        style.configure("TLabel", background="#f5f6f8")
        style.configure("TCheckbutton", background="#f5f6f8")
        style.configure("TLabelframe", background="#f5f6f8")
        style.configure("TLabelframe.Label", background="#f5f6f8", font=self.font_heading, foreground="#1769aa")
        style.configure("TNotebook", background="#f5f6f8")
        style.configure("TNotebook.Tab", font=self.font_heading, padding=(6, 6))
        style.configure("Treeview", font=self.font_base, rowheight=24)
        style.configure("Treeview.Heading", font=self.font_heading)
        style.configure(
            "Accent.TButton",
            font=self.font_heading,
            foreground="#ffffff",
            background="#1769aa",
            padding=(12, 4),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#0d47a1"), ("disabled", "#9bb8d3")],
            foreground=[("disabled", "#e8edf2")],
        )
        style.configure("Heading.TLabel", font=self.font_heading, foreground="#1769aa")
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Warn.TLabel", foreground="#c62828")
        style.configure("Big.TLabel", font=self.font_big, foreground="#1769aa")
        style.configure("Status.Info.TLabel", foreground="#1769aa", background="#e8edf2")
        style.configure("Status.Ok.TLabel", foreground="#2e7d32", background="#e6f0e6")
        style.configure("Status.Error.TLabel", foreground="#c62828", background="#f7e6e6")

    def _recolor_status(self, *_args):
        text = self.status_var.get()
        if "失败" in text or "无法" in text:
            level = "Error"
        elif "成功" in text or text.startswith("已"):
            level = "Ok"
        else:
            level = "Info"
        self.status_label.configure(style=f"Status.{level}.TLabel")

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        overview_page = self._scroll_page(self.notebook, "概览")
        item_page = ttk.Frame(self.notebook, padding=8)
        sugoroku_page = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(item_page, text="物品修改")
        self.notebook.add(sugoroku_page, text="双六屋")
        crew_page = self._scroll_page(self.notebook, "船员属性")
        settings_page = self._scroll_page(self.notebook, "修改器设置")
        self._build_settings(settings_page)

        overview_page.columnconfigure(0, weight=1)

        connection = ttk.LabelFrame(overview_page, text="游戏连接", padding=10)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        connection.columnconfigure(1, weight=1)
        connection_tools = ttk.Frame(connection)
        connection_tools.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(connection_tools, text="重新连接", style="Accent.TButton", command=self.connect).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(connection_tools, text="刷新数值", command=self.refresh).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(connection_tools, text="断开连接", command=self.disconnect).grid(row=0, column=2)
        connection_buttons = connection_tools.winfo_children()
        def layout_connection(event):
            columns = 3 if event.width >= 650 else 2
            for index, button in enumerate(connection_buttons):
                row, column = divmod(index, columns)
                button.grid_configure(row=row, column=column, sticky="w", pady=(0, 6))
        connection_tools.bind("<Configure>", layout_connection)
        ttk.Label(connection, textvariable=self.process_var, style="Heading.TLabel").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        resources = ttk.LabelFrame(overview_page, text="基础资源数据", padding=12)
        resources.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        resources.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate(RESOURCE_FIELDS):
            ttk.Label(resources, text=label).grid(row=row, column=0, padx=(0, 12), pady=5, sticky="w")
            entry = ttk.Entry(resources, textvariable=self.resource_vars[key], width=22)
            entry.grid(row=row, column=1, pady=5, sticky="ew")
            self.resource_entries[key] = entry
            ttk.Checkbutton(
                resources,
                text="锁定",
                variable=self.resource_lock_vars[key],
                command=lambda k=key: self.toggle_resource_lock(k),
            ).grid(row=row, column=2, padx=(12, 0), pady=5, sticky="w")
        ttk.Label(
            resources,
            text="体力修改的是当前体力，不修改体力上限；金钱输入上限 19 亿，给游戏收入结算留余量——写满 20 亿后游戏继续加钱会导致无法存档",
            style="Hint.TLabel",
        ).grid(
            row=len(RESOURCE_FIELDS), column=0, columnspan=2, pady=(8, 0), sticky="w"
        )
        resource_actions = ttk.Frame(resources)
        resource_actions.grid(row=len(RESOURCE_FIELDS) + 1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        resource_actions.columnconfigure(0, weight=1)
        ttk.Button(resource_actions, text="应用资源修改", style="Accent.TButton", command=self.apply_resources).grid(row=0, column=0, sticky="w")
        ttk.Label(resource_actions, text="仅修改本页四项基础资源；首次写入前自动备份存档。勾选“锁定”的资源会按目标值周期性写回，游戏内消耗或获得都会被改回；锁定期间资源可直接编辑，编辑值自动成为新目标", style="Hint.TLabel").grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(resource_actions, textvariable=self.resource_warn_var, style="Warn.TLabel").grid(row=2, column=0, sticky="ew", pady=(6, 0))

        summary = ttk.LabelFrame(overview_page, text="存档概况", padding=12)
        summary.grid(row=2, column=0, sticky="ew")
        ttk.Label(summary, textvariable=self.summary_var, style="Big.TLabel").pack(fill="x")
        ttk.Label(summary, textvariable=self.backup_hint_var, style="Hint.TLabel").pack(fill="x", pady=(6, 0))

        item_page.columnconfigure(0, weight=1)
        item_page.rowconfigure(0, weight=1)
        item_notebook = ttk.Notebook(item_page)
        item_notebook.grid(row=0, column=0, sticky="nsew")
        for tab, tab_name in enumerate(self.item_tab_names):
            self._build_item_tab(item_notebook, tab, tab_name)

        sugoroku_page.columnconfigure(0, weight=1)
        sugoroku_page.rowconfigure(0, weight=1)
        sugoroku_notebook = ttk.Notebook(sugoroku_page)
        sugoroku_notebook.grid(row=0, column=0, sticky="nsew")
        dice_tab = self._scroll_page(sugoroku_notebook, "投掷")
        card_tab = self._scroll_page(sugoroku_notebook, "卡片")

        dice_tab.columnconfigure(0, weight=1)
        dice_editor = ttk.LabelFrame(dice_tab, text="投掷设置", padding=12)
        dice_editor.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        dice_editor.columnconfigure(1, weight=1)
        ttk.Label(dice_editor, text="剩余投掷次数").grid(row=0, column=0, sticky="w", pady=4)
        self.dice_remaining_spinbox = ttk.Spinbox(
            dice_editor, from_=0, to=3, textvariable=self.dice_remaining_var, width=6
        )
        self.dice_remaining_spinbox.grid(row=0, column=1, padx=(6, 8), pady=4, sticky="w")
        self.dice_remaining_spinbox.bind("<Return>", lambda _event: self.apply_dice_remaining())
        ttk.Button(dice_editor, text="应用次数", command=self.apply_dice_remaining).grid(row=0, column=2, padx=(0, 12), pady=4)
        ttk.Checkbutton(
            dice_editor,
            text="锁定剩余次数",
            variable=self.lock_remaining_var,
            command=self.toggle_lock_remaining,
        ).grid(row=0, column=3, sticky="w", pady=4)

        ttk.Label(dice_editor, text="骰子数量").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(dice_editor, textvariable=self.dice_count_var, values=(1, 2, 3), state="readonly", width=5).grid(row=1, column=1, padx=(6, 8), pady=4, sticky="w")

        ttk.Label(dice_editor, text="总步数").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(dice_editor, from_=1, to=99, textvariable=self.dice_step_var, width=6).grid(row=2, column=1, padx=(6, 8), pady=4, sticky="w")
        ttk.Button(dice_editor, text="应用步数", command=self.apply_dice_step).grid(row=2, column=2, padx=(0, 12), pady=4)
        step_lock = ttk.Frame(dice_editor)
        step_lock.grid(row=2, column=3, sticky="w", pady=4)
        ttk.Checkbutton(
            step_lock,
            text="锁定步数",
            variable=self.lock_step_var,
            command=self.toggle_lock_step,
        ).pack(side="left")
        self.add_help(step_lock, "勾选后每次投掷都强制使用设定的总步数；不勾选则只对下一次投掷生效一次。99 是修改器的安全上限，不是游戏硬上限。").pack(side="left", padx=(4, 0))
        ttk.Label(dice_editor, text="总步数允许 1–99；多骰时不得小于骰子数量；应用后下一次投掷强制生效", style="Hint.TLabel").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(dice_editor, textvariable=self.board_info_var, style="Hint.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(dice_editor, text="刷新位置", command=self.refresh_board).grid(row=4, column=3, sticky="w", pady=(10, 0))

        ttk.Label(dice_editor, text="目标格").grid(row=5, column=0, sticky="w", pady=4)
        self.goto_spinbox = ttk.Spinbox(dice_editor, from_=1, to=200, textvariable=self.goto_var, width=6)
        self.goto_spinbox.grid(row=5, column=1, padx=(6, 8), pady=4, sticky="w")
        ttk.Button(dice_editor, text="走到目标格", command=self.apply_goto).grid(row=5, column=2, padx=(0, 12), pady=4)
        goto_tools = ttk.Frame(dice_editor)
        goto_tools.grid(row=5, column=3, sticky="w", pady=4)
        ttk.Button(goto_tools, text="取消目标", command=self.clear_goto).pack(side="left")
        self.add_help(
            goto_tools,
            "设置目标格后，每次投掷前会读取人物当前位置，按“目标格 - 当前格”计算步数并写入骰子；"
            "超过 99 步会自动保留设置、分多次投掷走过去；到达、越过目标格或切换关卡后自动失效。"
            "与“锁定步数”同时存在时优先走目标格；点过“应用步数”的单次步数优先于目标格。",
        ).pack(side="left", padx=(4, 0))
        ttk.Label(dice_editor, textvariable=self.goto_status_var, style="Hint.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))

        dice_rows = {0: 0, 1: 2, 2: 3, 3: 5, 4: 6, 5: 7, 6: 9}
        for child in dice_editor.winfo_children():
            position = child.grid_info()
            row, column = int(position["row"]), int(position["column"])
            span = int(position.get("columnspan", 1))
            if column == 3:
                target = {0: (1, 1), 2: (4, 1), 4: (6, 2), 5: (8, 1)}[row]
                child.grid_configure(row=target[0], column=target[1], columnspan=1, sticky="w")
            else:
                child.grid_configure(row=dice_rows[row], columnspan=2 if row == 4 else min(span, 3), sticky="ew" if column == 1 or span > 1 else "w")
        kairo_editor = ttk.LabelFrame(dice_tab, text="开罗点数", padding=12)
        kairo_editor.grid(row=1, column=0, sticky="ew")
        kairo_editor.columnconfigure(1, weight=1)
        ttk.Label(kairo_editor, text="开罗点数").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(kairo_editor, textvariable=self.kairo_point_var, width=10).grid(row=0, column=1, padx=8, pady=4, sticky="ew")
        ttk.Button(kairo_editor, text="应用开罗点数", style="Accent.TButton", command=self.apply_kairo_point).grid(row=0, column=2, pady=4)
        ttk.Label(kairo_editor, textvariable=self.kairo_point_limit_var, style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        card_tab.columnconfigure(0, weight=1)
        card_tab.rowconfigure(1, weight=1)
        card_tools = ttk.Frame(card_tab)
        card_tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(card_tools, text="扫描卡片", command=self.refresh).pack(side="left")
        ttk.Button(card_tools, text="全部勾选", command=self.select_all_cards).pack(side="left", padx=8)
        ttk.Button(card_tools, text="清空勾选", command=self.clear_card_selection).pack(side="left")

        card_list = ttk.Frame(card_tab)
        card_list.grid(row=1, column=0, sticky="nsew")
        card_list.columnconfigure(0, weight=1)
        card_list.rowconfigure(0, weight=1)
        self.card_tree = ttk.Treeview(
            card_list,
            columns=("checked", "name", "count", "maximum"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        self.card_tree.heading("checked", text="选择")
        self.card_tree.heading("name", text="卡片名称")
        self.card_tree.heading("count", text="当前数量")
        self.card_tree.heading("maximum", text="持有上限")
        self.card_tree.column("checked", width=50, minwidth=40, stretch=False, anchor="center")
        self.card_tree.column("name", width=280, minwidth=160, stretch=True, anchor="w")
        self.card_tree.column("count", width=100, minwidth=80, stretch=False, anchor="center")
        self.card_tree.column("maximum", width=110, minwidth=90, stretch=False, anchor="center")
        self.restore_column_widths()
        self.card_tree.grid(row=0, column=0, sticky="nsew")
        card_scroll = ttk.Scrollbar(card_list, orient="vertical", command=self.card_tree.yview)
        card_scroll.grid(row=0, column=1, sticky="ns")
        card_scroll_x = ttk.Scrollbar(card_list, orient="horizontal", command=self.card_tree.xview)
        card_scroll_x.grid(row=1, column=0, sticky="ew")
        self.card_tree.configure(yscrollcommand=card_scroll.set, xscrollcommand=card_scroll_x.set)
        self.card_tree.bind("<<TreeviewSelect>>", self.on_card_selected)
        self.card_tree.bind("<Button-1>", self.toggle_card_check)
        self.card_tree.bind("<ButtonRelease-1>", self.schedule_save_column_widths)

        card_editor = ttk.LabelFrame(card_tab, text="批量修改勾选卡片", padding=10)
        card_editor.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        card_editor.columnconfigure(1, weight=1)
        ttk.Label(card_editor, text="目标数量").grid(row=0, column=0, sticky="w")
        ttk.Entry(card_editor, textvariable=self.card_value_var, width=12).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(card_editor, text="修改数量并应用", style="Accent.TButton", command=self.apply_card_value).grid(row=0, column=2, sticky="e")
        ttk.Button(card_editor, text="一键设为上限 - 1", command=self.apply_card_safe_max).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.add_help(card_editor, "将勾选卡片分别设为各自的实时上限减 1（上限为 1 时直接设为 1），留出余量防止游戏内继续获得时溢出。不同卡片的上限独立计算。").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(card_tab, textvariable=self.card_limit_var, style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))

        crew_page.columnconfigure(0, weight=1)
        selector = ttk.Frame(crew_page)
        selector.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        selector.columnconfigure(1, weight=1)
        ttk.Label(selector, text="当前船员").grid(row=0, column=0, sticky="w")
        self.crew_selector = ttk.Combobox(selector, textvariable=self.crew_choice_var, state="readonly", width=20)
        self.crew_selector.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        self.crew_selector.bind("<<ComboboxSelected>>", self.on_crew_selected)

        basic = ttk.LabelFrame(crew_page, text="船员成长与战斗数值", padding=10)
        basic.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        basic.columnconfigure(1, weight=1)
        basic.columnconfigure(3, weight=1)
        basic_fields = []
        for index, (key, label) in enumerate((("level", "等级"), ("exp", "经验"), ("battleHP", "当前战斗 HP"), ("battleSP", "当前战斗 SP"))):
            row, column = divmod(index, 2)
            caption = ttk.Label(basic, text=label)
            entry = ttk.Entry(basic, textvariable=self.crew_vars[key], width=10)
            caption.grid(row=row, column=column * 2, padx=(0, 5), pady=4, sticky="w")
            entry.grid(row=row, column=column * 2 + 1, padx=(0, 8), pady=4, sticky="ew")
            basic_fields.append((caption, entry))
        ttk.Button(basic, text="战斗 HP/SP 恢复满", command=self.apply_crew_restore).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.add_help(
            basic,
            "按游戏当前计算出的 HP/SP 上限（基础+额外成长+职业成长+装备+技能+同伴/船只全部加成后的生效值），"
            "把当前船员的战斗 HP、战斗 SP 写满。只恢复战斗数值，不修改等级、经验和六项属性。",
        ).grid(row=2, column=2, sticky="w", padx=(6, 0), pady=(8, 0))

        restore_widgets = basic.winfo_children()[-2:]
        def layout_basic(event):
            groups = 2 if event.width >= 700 else 1
            basic.columnconfigure(3, weight=1 if groups == 2 else 0)
            for index, (caption, entry) in enumerate(basic_fields):
                row, group = divmod(index, groups)
                caption.grid_configure(row=row, column=group * 2)
                entry.grid_configure(row=row, column=group * 2 + 1)
            restore_row = 4 // groups
            restore_widgets[0].grid_configure(row=restore_row, column=0, columnspan=1)
            restore_widgets[1].grid_configure(row=restore_row, column=1)
        basic.bind("<Configure>", layout_basic)

        params = ttk.LabelFrame(crew_page, text="六项基础属性（param_）", padding=10)
        params.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        params.columnconfigure(1, weight=1)
        params.columnconfigure(3, weight=1)
        self._param_entries(params, self.param_vars)

        additions = ttk.LabelFrame(crew_page, text="六项额外成长（addParam_）", padding=10)
        additions.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        additions.columnconfigure(1, weight=1)
        additions.columnconfigure(3, weight=1)
        self._param_entries(additions, self.add_param_vars)
        for var in self.param_vars + self.add_param_vars:
            var.trace_add("write", lambda *_args: self.update_crew_headroom())

        crew_actions = ttk.Frame(crew_page)
        crew_actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        crew_actions.columnconfigure(0, weight=1)
        crew_actions.columnconfigure(1, weight=1)
        ttk.Button(crew_actions, text="应用船员修改", style="Accent.TButton", command=self.apply_crew).grid(row=0, column=0, sticky="w")
        ttk.Label(crew_actions, text="仅修改当前选中的 1 名船员；首次写入前自动备份存档", style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(crew_actions, text="一键拉满上限", command=self.apply_crew_max).grid(row=0, column=1, sticky="e")
        self.add_help(
            crew_actions,
            "一键将当前船员六项基础设为 10000、额外成长设为 9980000（HP/SP/攻/防/速 合计 9990000，"
            "智慧同值）；该值为探索/战斗安全上限——攻/防/速在战斗中会乘属性相性、HP/SP/速度会被技能"
            "百分比放大，两处都是 32 位乘法，合计超过约 1000 万就会溢出，导致打不动怪、被秒杀或恢复异常，"
            "探索直接失败。生效数值约 1000 万，已足够秒杀一切，且会显著降低游戏乐趣，请确认后再使用；"
            "首次写入前会自动备份存档。",
        ).grid(row=0, column=2, padx=(4, 0), sticky="e")
        self.crew_headroom_label = ttk.Label(crew_page, textvariable=self.crew_headroom_var, style="Hint.TLabel")
        self.crew_headroom_label.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Label(
            crew_page,
            text="同一属性 基础 + 成长 合计：HP/SP/攻/防/速 不要超过 9990000（战斗相性/技能百分比含 32 位乘法，"
            "超出会探索失败），智慧不要超过 2000000000（求和溢出归为 1）",
            style="Hint.TLabel",
        ).grid(row=6, column=0, sticky="w", pady=(4, 0))

        self.status_label = ttk.Label(root, textvariable=self.status_var, style="Status.Info.TLabel", relief="sunken", anchor="w", padding=6)
        self.status_label.grid(row=1, column=0, sticky="ew")
        self.status_var.trace_add("write", self._recolor_status)
        def wrap_labels(widget):
            if isinstance(widget, ttk.Label) and (widget.cget("style") in ("Hint.TLabel", "Big.TLabel", "Heading.TLabel") or widget is self.status_label):
                widget.configure(wraplength=120)
                if widget.winfo_manager() == "grid":
                    widget.grid_configure(sticky="ew")
                self._wrap(widget)
            for child in widget.winfo_children():
                wrap_labels(child)
        wrap_labels(root)

    def _build_item_tab(self, notebook, tab, tab_name):
        page = self._scroll_page(notebook, tab_name, padding=8)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        info = self.item_tabs[tab]

        tools = ttk.Frame(page)
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(tools, text="扫描背包", command=self.refresh).pack(side="left")
        ttk.Button(tools, text="全部勾选", command=lambda: self.select_all_items(tab)).pack(side="left", padx=8)
        ttk.Button(tools, text="清空勾选", command=lambda: self.clear_item_selection(tab)).pack(side="left")

        list_frame = ttk.Frame(page)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            list_frame,
            columns=("checked", "name", "count", "maximum"),
            show="headings",
            selectmode="browse",
        )
        tree.heading("checked", text="选择")
        tree.heading("name", text="物品名称")
        tree.heading("count", text="当前数量")
        tree.heading("maximum", text="当前持有上限")
        tree.column("checked", width=50, minwidth=40, stretch=False, anchor="center")
        tree.column("name", width=260, minwidth=160, stretch=True, anchor="w")
        tree.column("count", width=100, minwidth=80, stretch=False, anchor="center")
        tree.column("maximum", width=110, minwidth=90, stretch=False, anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(list_frame, orient="horizontal", command=tree.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=scroll_x.set)
        tree.bind("<<TreeviewSelect>>", lambda _event, t=tab: self.on_items_selected(t))
        tree.bind("<Button-1>", lambda event, t=tab: self.toggle_item_check(t, event))
        tree.bind("<ButtonRelease-1>", self.schedule_save_column_widths)
        info["tree"] = tree

        editor = ttk.LabelFrame(page, text=f"批量修改勾选{tab_name}", padding=10)
        editor.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        editor.columnconfigure(1, weight=1)
        ttk.Label(editor, text="目标数量").grid(row=0, column=0, sticky="w")
        ttk.Entry(editor, textvariable=info["value_var"], width=12).grid(row=0, column=1, padx=8, sticky="ew")
        ttk.Button(editor, text="修改数值并应用", style="Accent.TButton", command=lambda: self.apply_item_value(tab)).grid(row=0, column=2, sticky="e")
        ttk.Button(editor, text="一键设为上限 - 9", command=lambda: self.apply_item_safe_max(tab)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.add_help(editor, "将勾选物品分别设为各自的实时上限减 9，留出余量防止游戏内继续获得时溢出。不同物品的上限独立计算。").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(page, textvariable=info["limit_var"], style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))

    def _param_entries(self, parent, variables):
        for index, name in enumerate(PARAM_NAMES):
            row, group = divmod(index, 2)
            ttk.Label(parent, text=name).grid(row=row, column=group * 2, padx=(0, 6), pady=3, sticky="w")
            ttk.Entry(parent, textvariable=variables[index], width=10).grid(row=row, column=group * 2 + 1, padx=(0, 8), pady=3, sticky="ew")

    def connect(self):
        if self._busy is not None or self._closing or self._close_failed:
            if self._busy is not None and not self._closing:
                self.status_var.set(f"正在{self._busy}，请等待完成")
            return
        self._reset_connection()
        def run(game):
            game.connect()
            try:
                return game.pid, game.read_state(), None
            except Exception as exc:
                return game.pid, None, str(exc)
        def done(result):
            pid, state, error = result
            self._connected = True
            self.process_var.set(f"已连接：{GAME_NAME}（PID {pid}）")
            if error:
                self.status_var.set(f"游戏已连接，读取失败：{error}")
                messagebox.showwarning("游戏已连接", f"读取失败：{error}\n进入存档后可手动刷新。", parent=self)
            else:
                self.update_state(state)
                self.status_var.set("已连接并读取全部数据")
        self._submit("连接游戏", run, done, require_connection=False)

    def refresh(self):
        def done(state):
            self.update_state(state)
            self.status_var.set("已刷新资源、船员、三个物品页与双六数据")
        self._submit("刷新全部数值", lambda game: game.read_state(), done)

    def update_state(self, state):
        self.snapshot = state
        for key in self.resource_vars:
            self.resource_vars[key].set(str(state[key]))
        self._update_resource_warning()
        self.update_items(dict(enumerate(state["itemsByTab"])))
        sugoroku = state["sugoroku"]
        self._show_sugoroku_step(sugoroku)
        self._show_sugoroku_remaining(sugoroku)
        self._show_kairo_point(sugoroku)
        self._update_board_ui(sugoroku.get("board") or {})
        self._show_cards(sugoroku["cards"])
        self._update_summary()
        self._update_crew_choices()
        if state["crews"]:
            self.load_crew_form()

    def _update_summary(self):
        if not self.snapshot:
            return
        items = self.snapshot["itemsByTab"]
        self.summary_var.set(
            f"{len(self.snapshot['crews'])} 名船员  ·  武器 {len(items[0])} 件  ·  道具 {len(items[1])} 件  ·  "
            f"强化食物 {len(items[2])} 件  ·  {len(self.snapshot['sugoroku']['cards'])} 张双六卡片"
        )

    def _update_crew_choices(self):
        choices = [
            f"{crew['index'] + 1}. {self.display_name(crew['name'])}  Lv.{crew['level']}"
            for crew in self.snapshot["crews"]
        ]
        self.crew_selector.configure(values=choices)
        if not choices:
            self.crew_position = 0
            self.crew_choice_var.set("当前存档没有可编辑船员")
            for var in list(self.crew_vars.values()) + self.param_vars + self.add_param_vars:
                var.set("")
            self.crew_headroom_var.set("当前存档没有可编辑船员")
            return
        self.crew_position = min(self.crew_position, len(choices) - 1)
        self.crew_selector.current(self.crew_position)

    def update_items(self, items_by_tab):
        for tab, items in items_by_tab.items():
            info = self.item_tabs[tab]
            tree = info["tree"]
            selected = set(tree.selection())
            valid_ids = {str(item["id"]) for item in items}
            info["checked"].intersection_update(valid_ids)
            tree.delete(*tree.get_children())
            for item in items:
                iid = str(item["id"])
                tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        "☑" if iid in info["checked"] else "☐",
                        self.display_name(item["name"], "未命名物品"),
                        item["count"],
                        item["maximum"],
                    ),
                )
            remaining = tuple(sorted(selected.intersection(valid_ids), key=int))
            tree.selection_set(remaining)
            self.on_items_selected(tab)

    def _apply_items_readback(self, tab, items):
        if self.snapshot is None:
            return
        self.snapshot["itemsByTab"][tab] = items
        self.update_items({tab: items})
        self._update_summary()

    def selected_item_ids(self, tab):
        """返回勾选的物品 id（不是列表下标）。"""
        info = self.item_tabs[tab]
        return sorted(int(iid) for iid in info["checked"] if info["tree"].exists(iid))

    def set_item_checked(self, tab, iid, checked):
        info = self.item_tabs[tab]
        if checked:
            info["checked"].add(iid)
        else:
            info["checked"].discard(iid)
        info["tree"].set(iid, "checked", "☑" if checked else "☐")
        self.on_items_selected(tab)

    def toggle_item_check(self, tab, event):
        tree = self.item_tabs[tab]["tree"]
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return None
        iid = tree.identify_row(event.y)
        if iid:
            self.set_item_checked(tab, iid, iid not in self.item_tabs[tab]["checked"])
        return "break"

    def update_item_check_marks(self, tab):
        info = self.item_tabs[tab]
        for iid in info["tree"].get_children():
            info["tree"].set(iid, "checked", "☑" if iid in info["checked"] else "☐")

    def select_all_items(self, tab):
        info = self.item_tabs[tab]
        info["checked"] = set(info["tree"].get_children())
        self.update_item_check_marks(tab)
        self.on_items_selected(tab)

    def clear_item_selection(self, tab):
        info = self.item_tabs[tab]
        info["checked"].clear()
        info["tree"].selection_set()
        self.update_item_check_marks(tab)
        self.on_items_selected(tab)

    def on_items_selected(self, tab):
        info = self.item_tabs[tab]
        ids = self.selected_item_ids(tab)
        if not ids:
            info["limit_var"].set("请点击“选择”列的复选框；可勾选多个物品")
            return
        items = {item["id"]: item for item in self.snapshot["itemsByTab"][tab]}
        maximum = min(items[item_id]["maximum"] for item_id in ids)
        info["limit_var"].set(f"已勾选 {len(ids)} 项；本次可输入 0 到 {maximum}，应用时会再次实时检测")
        if len(ids) == 1:
            info["value_var"].set(str(items[ids[0]]["count"]))

    def apply_item_value(self, tab):
        ids = self.selected_item_ids(tab)
        if not ids:
            messagebox.showerror("无法应用", "请先勾选要修改的物品", parent=self)
            return
        try:
            items = {item["id"]: item for item in self.snapshot["itemsByTab"][tab]}
            maximum = min(items[item_id]["maximum"] for item_id in ids)
            value = self.parse_number(self.item_tabs[tab]["value_var"].get(), "物品数量", maximum)
            self._submit_write(
                "物品修改",
                write=lambda game: game.apply_tab_items(tab, ids, value=value),
                read_back=lambda game: game.read_items(tab),
                apply_readback=lambda items: self._apply_items_readback(tab, items),
                message=f"已将 {len(ids)} 种物品数量写为 {value}，并完成本页回读",
            )
        except Exception as exc:
            self.status_var.set("物品修改失败")
            messagebox.showerror("物品修改失败", str(exc), parent=self)

    def apply_item_safe_max(self, tab):
        ids = self.selected_item_ids(tab)
        if not ids:
            messagebox.showerror("无法应用", "请先勾选要修改的物品", parent=self)
            return
        try:
            self._submit_write(
                "物品修改",
                write=lambda game: game.apply_tab_items(tab, ids, safe_max=True),
                read_back=lambda game: game.read_items(tab),
                apply_readback=lambda items: self._apply_items_readback(tab, items),
                message=f"已将 {len(ids)} 种物品设为各自实时上限 - 9，并完成本页回读",
            )
        except Exception as exc:
            self.status_var.set("物品修改失败")
            messagebox.showerror("物品修改失败", str(exc), parent=self)

    def _apply_sugoroku_readback(self, data, show):
        if self.snapshot is not None:
            self.snapshot["sugoroku"].update(data)
        show(data)

    def _show_sugoroku_step(self, data):
        locked = data["stepLocked"]
        self.lock_step_var.set(locked)
        if locked:
            self.dice_count_var.set(str(data["stepLockDiceCount"]))
            self.dice_step_var.set(str(data["stepLockTotal"]))
        else:
            self.dice_step_var.set(str(data["step"]))

    def _show_sugoroku_remaining(self, data):
        self.dice_remaining_var.set(str(data["remaining"]))
        self.dice_remaining_spinbox.configure(to=data["maximum"])

    def _show_kairo_point(self, data):
        self.kairo_point_var.set(str(data["kairoPoint"]))
        if data["kairoReady"]:
            self.kairo_point_limit_var.set("可输入 0 到 2147483647，写入时通过游戏自身的数值对象安全应用")
        else:
            self.kairo_point_limit_var.set("开罗点数尚未初始化，请进入正常游戏界面后刷新")

    def _apply_cards_readback(self, data):
        self._apply_sugoroku_readback(data, lambda state: self._show_cards(state["cards"]))
        self._update_summary()

    def _show_cards(self, cards):
        selected = set(self.card_tree.selection())
        valid_ids = {str(card["index"]) for card in cards}
        self.checked_cards.intersection_update(valid_ids)
        self.card_tree.delete(*self.card_tree.get_children())
        for card in cards:
            iid = str(card["index"])
            self.card_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "☑" if iid in self.checked_cards else "☐",
                    self.display_name(card["name"], "未命名卡片"),
                    card["count"],
                    card["maximum"],
                ),
            )
        children = self.card_tree.get_children()
        remaining = tuple(sorted(selected.intersection(children), key=int))
        self.card_tree.selection_set(remaining)
        self.on_card_selected()

    def selected_card_indices(self):
        return sorted(int(iid) for iid in self.checked_cards if self.card_tree.exists(iid))

    def set_card_checked(self, iid, checked):
        if checked:
            self.checked_cards.add(iid)
        else:
            self.checked_cards.discard(iid)
        self.card_tree.set(iid, "checked", "☑" if checked else "☐")
        self.on_card_selected()

    def toggle_card_check(self, event):
        if self.card_tree.identify_region(event.x, event.y) != "cell" or self.card_tree.identify_column(event.x) != "#1":
            return None
        iid = self.card_tree.identify_row(event.y)
        if iid:
            self.set_card_checked(iid, iid not in self.checked_cards)
        return "break"

    def update_card_check_marks(self):
        for iid in self.card_tree.get_children():
            self.card_tree.set(iid, "checked", "☑" if iid in self.checked_cards else "☐")

    def select_all_cards(self):
        self.checked_cards = set(self.card_tree.get_children())
        self.update_card_check_marks()
        self.on_card_selected()

    def clear_card_selection(self):
        self.checked_cards.clear()
        self.card_tree.selection_set()
        self.update_card_check_marks()
        self.on_card_selected()

    def on_card_selected(self, _event=None):
        indices = self.selected_card_indices()
        if not indices:
            if not self.card_tree.get_children():
                self.card_limit_var.set("当前没有可修改的卡片；若尚未进入双六屋，请进入后点击“刷新数值”")
            else:
                self.card_limit_var.set("请点击“选择”列的复选框；可勾选多张卡片批量修改")
            return
        if not self.snapshot:
            return
        cards = {card["index"]: card for card in self.snapshot["sugoroku"]["cards"]}
        maximum = min(cards[index]["maximum"] for index in indices)
        self.card_limit_var.set(f"已勾选 {len(indices)} 张卡片；本次可输入 0 到 {maximum}，应用时会再次实时检测")
        if len(indices) == 1:
            self.card_value_var.set(str(cards[indices[0]]["count"]))

    def apply_card_value(self):
        indices = self.selected_card_indices()
        if not indices:
            messagebox.showerror("无法应用", "请先勾选要修改的卡片", parent=self)
            return
        try:
            cards = {card["index"]: card for card in self.snapshot["sugoroku"]["cards"]}
            maximum = min(cards[index]["maximum"] for index in indices)
            value = self.parse_number(self.card_value_var.get(), "卡片数量", maximum)
            self._submit_write(
                "卡片修改",
                write=lambda game: game.apply_cards(indices, value=value),
                read_back=lambda game: game.read_sugoroku_cards(),
                apply_readback=self._apply_cards_readback,
                message=f"已将 {len(indices)} 张卡片数量写为 {value}，并完成卡片回读",
            )
        except Exception as exc:
            self.status_var.set("卡片修改失败")
            messagebox.showerror("卡片修改失败", str(exc), parent=self)

    def apply_card_safe_max(self):
        indices = self.selected_card_indices()
        if not indices:
            messagebox.showerror("无法应用", "请先勾选要修改的卡片", parent=self)
            return
        try:
            self._submit_write(
                "卡片修改",
                write=lambda game: game.apply_cards(indices, safe_max=True),
                read_back=lambda game: game.read_sugoroku_cards(),
                apply_readback=self._apply_cards_readback,
                message=f"已将 {len(indices)} 张卡片设为各自实时上限 - 1，并完成卡片回读",
            )
        except Exception as exc:
            self.status_var.set("卡片修改失败")
            messagebox.showerror("卡片修改失败", str(exc), parent=self)

    def apply_dice_step(self):
        try:
            count = self.parse_number(self.dice_count_var.get(), "骰子数量", 3, 1)
            value = self.parse_number(self.dice_step_var.get(), "总步数", 99, count)
            locked = self.lock_step_var.get()
            def write(game):
                if locked:
                    game.set_step_lock(value, count)
                else:
                    game.set_next_roll_steps(value, count)

            self._submit_write(
                "步数修改",
                write=write,
                read_back=lambda game: game.read_sugoroku_step(),
                apply_readback=lambda data: self._apply_sugoroku_readback(data, self._show_sugoroku_step),
                message=f"已设置 {count} 颗骰子合计 {value} 步（{'持续锁定' if locked else '下一次投掷'}）",
            )
        except Exception as exc:
            messagebox.showerror("步数修改失败", str(exc), parent=self)

    def toggle_lock_step(self):
        enabled = self.lock_step_var.get()
        try:
            count = self.parse_number(self.dice_count_var.get(), "骰子数量", 3, 1) if enabled else 1
            value = self.parse_number(self.dice_step_var.get(), "总步数", 99, count) if enabled else 0
            def run(game):
                if enabled:
                    return game.set_step_lock(value, count)
                return game.clear_step_lock()
            def done(_result):
                self.lock_step_var.set(enabled)
                if self.snapshot:
                    self.snapshot["sugoroku"]["stepLocked"] = enabled
                self.status_var.set(f"已启用步数锁定：{count} 颗骰子合计 {value} 步" if enabled else "已停用步数锁定")
            accepted = self._submit("设置步数锁定", run, done, lambda _exc: self.lock_step_var.set(not enabled))
            if not accepted:
                self.lock_step_var.set(not enabled)
        except Exception as exc:
            self.lock_step_var.set(not enabled)
            messagebox.showerror("步数锁定失败", str(exc), parent=self)

    def apply_goto(self):
        try:
            if not self.snapshot:
                raise ValueError("请先刷新读取数据")
            board = self.snapshot["sugoroku"].get("board") or {}
            if not board.get("ready"):
                raise ValueError("双六对局数据尚未初始化，请进入双六屋后刷新")
            grid_max = board.get("gridMax", 0)
            target = self.parse_number(self.goto_var.get(), "目标格", grid_max, 1)
            current = board.get("pos", 0)
            if target <= current:
                raise ValueError(f"目标格必须大于当前所在格数（当前第 {current} 格）")
            def done(result):
                self.goto_status_var.set(
                    f"目标：第 {target} 格（第 {result['level'] + 1} 关，当前第 {result['pos']} 格，本关共 {result['gridMax']} 格）"
                )
                self.status_var.set(f"已设置目标格 {target}，下一次投掷将自动走过去")
            self._submit("设置目标格", lambda game: game.set_goto_cell(target), done)
        except Exception as exc:
            self.status_var.set("目标格设置失败")
            messagebox.showerror("目标格设置失败", str(exc), parent=self)

    def clear_goto(self):
        def done(_result):
            self.goto_status_var.set("已取消目标格")
            self.status_var.set("已取消目标格")
        self._submit("取消目标格", lambda game: game.clear_goto_cell(), done)

    def _update_board_ui(self, board):
        if board.get("ready"):
            grid_max = board.get("gridMax", 0)
            self.board_info_var.set(
                f"当前棋盘：第 {board['level'] + 1} 关（本关共 {grid_max} 格）· 当前第 {board['pos']} 格"
            )
            self.goto_spinbox.configure(to=grid_max if grid_max > 0 else 200)
        else:
            self.board_info_var.set("双六对局数据尚未初始化，进入双六屋一次后刷新")
            self.goto_spinbox.configure(to=200)

    def refresh_board(self):
        def done(info):
            board = info.get("board") or {}
            if self.snapshot is not None:
                self.snapshot["sugoroku"]["board"] = board
            self._update_board_ui(board)
            notice = info.get("gotoNotice")
            if notice:
                self.goto_status_var.set(notice)
            elif info.get("gotoTarget") is not None:
                self.goto_status_var.set(f"目标：第 {info['gotoTarget']} 格（已设置，等待下一次投掷）")
            self.status_var.set("已刷新棋盘位置")
        self._submit("刷新棋盘", lambda game: game.read_board_state(), done)

    def apply_dice_remaining(self):
        try:
            if not self.snapshot:
                raise ValueError("请先刷新读取数据")
            maximum = self.snapshot["sugoroku"].get("maximum", 3)
            value = self.parse_number(self.dice_remaining_var.get(), "剩余投掷次数", maximum)
            def on_written():
                if self.lock_remaining_var.get():
                    self._lock_target_value = value
                    self._lock_failures = 0

            if not self._submit_write(
                "次数修改",
                write=lambda game: game.set_sugoroku_remaining(value),
                read_back=lambda game: game.read_sugoroku_remaining(),
                apply_readback=lambda data: self._apply_sugoroku_readback(data, self._show_sugoroku_remaining),
                message=f"已将剩余投掷次数设为 {value}，并完成范围回读",
                on_written=on_written,
            ):
                self.status_var.set("操作被占用，请稍后重试")
        except Exception as exc:
            messagebox.showerror("次数修改失败", str(exc), parent=self)

    def apply_kairo_point(self):
        try:
            value = self.parse_number(self.kairo_point_var.get(), "开罗点数", 2147483647)
            self._submit_write(
                "开罗点数修改",
                write=lambda game: game.write_kairo_point(value),
                read_back=lambda game: game.read_kairo_point(),
                apply_readback=lambda data: self._apply_sugoroku_readback(data, self._show_kairo_point),
                message=f"已将开罗点数设为 {value}，并完成范围回读",
            )
        except Exception as exc:
            messagebox.showerror("开罗点数修改失败", str(exc), parent=self)

    def toggle_lock_remaining(self):
        enabled = self.lock_remaining_var.get()
        if not enabled:
            self._stop_lock_remaining()
            self.status_var.set("已停用剩余投掷次数锁定")
            return
        try:
            if not self.snapshot:
                raise ValueError("请先刷新读取数据")
            maximum = self.snapshot["sugoroku"].get("maximum", 3)
            target = self.parse_number(self.dice_remaining_var.get(), "剩余投掷次数", maximum)
            def done(result):
                self._lock_target_value = target
                self._lock_failures = 0
                self.dice_remaining_var.set(str(result["remaining"]))
                self.status_var.set(f"已启用剩余投掷次数锁定（目标 {target}，每 {self._lock_interval() / 1000:g} 秒检查）")
                self._schedule_lock()
            def failed(_error):
                self.lock_remaining_var.set(False)
                self._stop_lock_remaining()
            if not self._submit("启用次数锁定", lambda game: game.write_sugoroku_remaining(target), done, failed):
                self.lock_remaining_var.set(False)
        except Exception as exc:
            self.lock_remaining_var.set(False)
            self._stop_lock_remaining()
            messagebox.showerror("锁定失败", str(exc), parent=self)

    def _lock_interval(self):
        return self.settings.get("lock_interval_ms", DEFAULT_LOCK_INTERVAL_MS)

    def _schedule_lock(self):
        if self._lock_timer_id is not None:
            self.after_cancel(self._lock_timer_id)
        if self.lock_remaining_var.get() and not self._closing:
            self._lock_timer_id = self.after(self._lock_interval(), self._lock_remaining_loop)

    def _lock_remaining_loop(self):
        self._lock_timer_id = None
        if not self.lock_remaining_var.get() or self._closing or not self._connected:
            return
        if self._busy is not None:
            self._schedule_lock()
            return
        target = self._lock_target_value
        def run(game):
            info = game.read_sugoroku_remaining()
            value = min(target, info["maximum"])
            if info["remaining"] != value:
                info = game.write_sugoroku_remaining(value)
            return info, value
        def done(result):
            info, value = result
            self._lock_target_value = value
            self._lock_failures = 0
            if self.snapshot:
                self.snapshot["sugoroku"]["remaining"] = info["remaining"]
            self._schedule_lock()
        def failed(error):
            self._lock_failures += 1
            self.status_var.set(f"锁定检查失败（{self._lock_failures}/3）：{error}")
            if self._lock_failures >= 3:
                self.lock_remaining_var.set(False)
                self._stop_lock_remaining()
                self.status_var.set("锁定已停止：连续读取或写入失败，请进入游戏并刷新数值")
            else:
                self._schedule_lock()
        self._submit("检查次数锁定", run, done, failed, quiet=True, background=True)

    def _stop_lock_remaining(self):
        if self._lock_timer_id is not None:
            self.after_cancel(self._lock_timer_id)
            self._lock_timer_id = None
        self._lock_failures = 0

    def load_settings(self):
        try:
            return self.settings_store.load()
        except Exception as exc:
            self._settings_error = str(exc)
            return {"backup_root": str(DEFAULT_BACKUP_ROOT), "lock_interval_ms": DEFAULT_LOCK_INTERVAL_MS, "auto_connect": True}

    def _build_settings(self, page):
        page.columnconfigure(0, weight=1)
        backup = ttk.LabelFrame(page, text="存档备份", padding=12)
        backup.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        backup.columnconfigure(0, weight=1)
        ttk.Label(backup, text="备份目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(backup, textvariable=self.backup_root_var).grid(row=1, column=0, sticky="ew", pady=(6, 0), padx=(0, 8))
        ttk.Button(backup, text="浏览……", command=self.browse_backup_root).grid(row=1, column=1, pady=(6, 0))
        ttk.Label(backup, text="首次游戏写入前自动备份；备份目录必须位于游戏安装目录之外。", style="Hint.TLabel").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        behavior = ttk.LabelFrame(page, text="连接与锁定", padding=12)
        behavior.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        behavior.columnconfigure(1, weight=1)
        ttk.Label(behavior, text="锁定轮询间隔（秒）").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Spinbox(behavior, from_=0.2, to=10, increment=0.1, textvariable=self.lock_interval_var, width=8).grid(row=0, column=1, sticky="ew")
        ttk.Label(behavior, text="允许 0.2–10 秒，影响剩余投掷次数锁定与基础资源锁定的检查频率。", style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(behavior, text="启动时自动连接游戏", variable=self.auto_connect_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(page, text="保存设置", style="Accent.TButton", command=self.save_preferences).grid(row=2, column=0, sticky="w")

    def browse_backup_root(self):
        path = filedialog.askdirectory(parent=self, title="选择存档备份目录", initialdir=self.backup_root_var.get() or str(DEFAULT_BACKUP_ROOT))
        if path:
            self.backup_root_var.set(path)

    def save_preferences(self):
        try:
            root = validate_backup_root(self.backup_root_var.get())
            seconds = float(self.lock_interval_var.get())
            if not 0.2 <= seconds <= 10:
                raise ValueError("锁定轮询间隔必须在 0.2 到 10 秒之间")
            interval = round(seconds * 1000)
            def run(game):
                old_root = game.backup_root
                game.set_backup_root(root)
                return old_root
            def done(old_root):
                data = dict(self.settings)
                data.update(backup_root=str(root), lock_interval_ms=interval, auto_connect=self.auto_connect_var.get())
                try:
                    self.settings_store.save(data)
                except Exception as exc:
                    self._submit("恢复备份目录设置", lambda game: game.set_backup_root(old_root), require_connection=False)
                    messagebox.showerror("设置保存失败", str(exc), parent=self)
                    return
                self.settings = data
                self.backup_root_var.set(str(root))
                self.backup_hint_var.set(f"存档备份目录：{root}")
                self.lock_interval_var.set(f"{interval / 1000:g}")
                if self.lock_remaining_var.get():
                    self._schedule_lock()
                if self._resource_lock_targets:
                    self._schedule_resource_lock()
                self.status_var.set(f"已保存设置；锁定检查间隔 {interval / 1000:g} 秒")
            self._submit("保存设置", run, done, require_connection=False)
        except Exception as exc:
            messagebox.showerror("设置无效", str(exc), parent=self)

    def restore_column_widths(self):
        pairs = [(self.card_tree, self.settings.get("card_columns"))]
        by_tab = self.settings.get("item_columns_by_tab", {})
        pairs += [(info["tree"], by_tab.get(str(tab))) for tab, info in self.item_tabs.items()]
        for tree, saved in pairs:
            if not isinstance(saved, dict):
                continue
            for name in ("checked", "name", "count", "maximum"):
                width = saved.get(name)
                if isinstance(width, int) and 20 <= width <= 2000:
                    tree.column(name, width=width)

    def schedule_save_column_widths(self, _event=None):
        if self._closing:
            return
        if self._column_save_timer_id is not None:
            self.after_cancel(self._column_save_timer_id)
        self._column_save_timer_id = self.after(350, self.save_column_widths)

    def _capture_columns(self):
        def widths(tree):
            return {name: int(tree.column(name, "width")) for name in ("checked", "name", "count", "maximum")}
        self.settings["card_columns"] = widths(self.card_tree)
        self.settings["item_columns_by_tab"] = {str(tab): widths(info["tree"]) for tab, info in self.item_tabs.items()}

    def save_column_widths(self):
        self._column_save_timer_id = None
        self._capture_columns()
        self._persist_settings()

    def _persist_settings(self):
        try:
            self.settings_store.save(dict(self.settings))
        except Exception as exc:
            self.status_var.set(f"设置保存失败：{exc}")
            return False
        return True

    def _restore_window(self):
        saved = self.settings.get("window_geometry", "")
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", saved) if isinstance(saved, str) else None
        if match:
            width, height, x, y = map(int, match.groups())
            screen_width, screen_height = self.winfo_screenwidth(), self.winfo_screenheight()
            width = max(640, min(width, screen_width))
            height = max(480, min(height, screen_height))
            x = max(0, min(x, screen_width - width))
            y = max(0, min(y, screen_height - height))
            self.geometry(f"{width}x{height}+{x}+{y}")
            self._normal_geometry = f"{width}x{height}+{x}+{y}"
        self._geometry_ready = False
        self._last_window_state = "zoomed" if self.settings.get("window_maximized", False) else "normal"
        if self._last_window_state == "zoomed":
            self.state("zoomed")
        self._geometry_enable_timer_id = self.after(800, self._enable_geometry_tracking)

    def _enable_geometry_tracking(self):
        self._geometry_enable_timer_id = None
        self._geometry_ready = True
        self._capture_geometry()

    def _window_changed(self, event):
        if event.widget is not self or self._closing or not getattr(self, "_geometry_ready", False):
            return
        current = self.state()
        if current not in ("normal", "zoomed"):
            return
        self._capture_geometry()
        if self._geometry_timer_id is not None:
            self.after_cancel(self._geometry_timer_id)
        self._geometry_timer_id = self.after(600, self._save_geometry)

    def _capture_geometry(self):
        current = self.state()
        if current == "normal":
            self._normal_geometry = self.geometry()
        self.settings["window_geometry"] = self._normal_geometry
        if current in ("normal", "zoomed"):
            self._last_window_state = current
        self.settings["window_maximized"] = self._last_window_state == "zoomed"

    def _save_geometry(self):
        self._geometry_timer_id = None
        self._capture_geometry()
        self._persist_settings()

    def display_name(self, name, fallback="未命名船员"):
        cleaned = "".join(char for char in name if char.isprintable()).strip()
        return cleaned or fallback

    def on_crew_selected(self, _event):
        if self.snapshot and self.snapshot["crews"]:
            self.crew_position = self.crew_selector.current()
            self.load_crew_form()

    def load_crew_form(self):
        if not self.snapshot or not self.snapshot["crews"]:
            return
        crew = self.snapshot["crews"][self.crew_position]
        for key in self.crew_vars:
            self.crew_vars[key].set(str(crew[key]))
        for index in range(6):
            self.param_vars[index].set(str(crew["params"][index]))
            self.add_param_vars[index].set(str(crew["addParams"][index]))
        self.update_crew_headroom()

    def update_crew_headroom(self):
        try:
            totals = []
            for i in range(6):
                base_text = self.param_vars[i].get().strip()
                growth_text = self.add_param_vars[i].get().strip()
                base = int(base_text) if base_text else 0
                growth = int(growth_text) if growth_text else 0
                totals.append(base + growth)
        except ValueError:
            self.crew_headroom_var.set("⚠ 属性输入包含非数字内容，无法估算余量")
            self.crew_headroom_label.configure(foreground="#c62828")
            return
        limits = [PARAM_SAFE_TOTAL if PARAM_NAMES[i] in PARAM_SAFE_LIMITED else PARAM_SUM_LIMIT for i in range(6)]
        over = [PARAM_NAMES[i] for i, total in enumerate(totals) if total > limits[i]]
        parts = []
        for i, total in enumerate(totals):
            mark = "*" if PARAM_NAMES[i] in PARAM_SAFE_LIMITED else ""
            parts.append(f"{PARAM_NAMES[i]}{mark} 剩 {limits[i] - total:,}")
        text = "距安全上限余量：" + "，".join(parts)
        if over:
            text = "⚠ " + "、".join(over) + " 合计已超安全上限，应用会被拒绝。" + text
            self.crew_headroom_label.configure(foreground="#c62828")
        else:
            text += "（* 项探索/战斗含 32 位乘法，安全上限 999 万）"
            self.crew_headroom_label.configure(foreground="#6b7280")
        self.crew_headroom_var.set(text)

    def parse_number(self, value, label, maximum=2_000_000_000, minimum=0):
        try:
            number = int(value.strip(), 10)
        except ValueError as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{label}必须在 {minimum:,} 到 {maximum:,} 之间")
        return number

    def apply_resources(self):
        if not self.snapshot:
            self.status_var.set("请先刷新读取数据")
            return
        changes = {}
        skipped = []
        for key, label in RESOURCE_FIELDS:
            try:
                maximum = RESOURCE_INPUT_MAX.get(key, RESOURCE_SAFE_MAX)
                changes[key] = self.parse_number(self.resource_vars[key].get(), label, maximum)
            except ValueError as exc:
                # 超输入上限或非法的项只跳过该项，不阻塞其它资源的写入；提示只进状态栏，不弹窗。
                skipped.append(str(exc))
        if not changes:
            self.status_var.set("没有可写入的资源（全部已跳过）：" + "；".join(skipped))
            return
        def on_written():
            for key, value in changes.items():
                if key in self._resource_lock_targets:
                    self._resource_lock_targets[key] = value
        message = f"已将 {len(changes)} 项基础资源写入并完成回读"
        if skipped:
            message += "；已跳过 " + "；".join(skipped)
        if not self._submit_write(
            "资源应用",
            write=lambda game: game.write_resources(changes),
            read_back=lambda game: game.read_resources(),
            apply_readback=lambda data: self._apply_resources_readback(data, changes),
            message=message,
            on_written=on_written,
        ):
            self.status_var.set("资源应用被占用，请稍后重试")

    def _apply_resources_readback(self, data, submitted_keys):
        if self.snapshot is not None:
            self.snapshot.update(data)
        for key in submitted_keys:
            self.resource_vars[key].set(str(data[key]))
        self._update_resource_warning()

    def _update_resource_warning(self):
        if not self.snapshot:
            self.resource_warn_var.set("")
            return
        problems = []
        for key, label in RESOURCE_FIELDS:
            value = self.snapshot.get(key)
            if not isinstance(value, int):
                continue
            if value < 0:
                problems.append(f"{label}当前为 {value}（负数，可能已溢出）")
            elif value > RESOURCE_SAFE_MAX:
                problems.append(f"{label}当前 {value:,}，超出上限 {RESOURCE_SAFE_MAX:,}")
        if not problems:
            self.resource_warn_var.set("")
            return
        self.resource_warn_var.set(
            "⚠ " + "；".join(problems) + "。游戏存档时校验全部数值，超限会导致无法保存，请改回上限以内再存档"
        )

    def toggle_resource_lock(self, key):
        label = self._resource_labels[key]
        enabled = self.resource_lock_vars[key].get()
        if not enabled:
            self._resource_lock_targets.pop(key, None)
            self._schedule_resource_lock()
            self.status_var.set(f"已停用{label}锁定")
            return
        try:
            if not self.snapshot:
                raise ValueError("请先刷新读取数据")
            maximum = RESOURCE_INPUT_MAX.get(key, RESOURCE_SAFE_MAX)
            target = self.parse_number(self.resource_vars[key].get(), label, maximum)
            def run(game):
                return game.write_resources({key: target})
            def done(_result):
                self._resource_lock_targets[key] = target
                self._resource_lock_failures = 0
                self.status_var.set(
                    f"已启用{label}锁定（目标 {target}，每 {self._lock_interval() / 1000:g} 秒检查）"
                )
                self._schedule_resource_lock()
            def failed(error):
                self.resource_lock_vars[key].set(False)
                self.status_var.set(f"启用{label}锁定失败：{error}")
            if not self._submit(f"启用{label}锁定", run, done, failed, quiet=True):
                self.resource_lock_vars[key].set(False)
        except Exception as exc:
            self.resource_lock_vars[key].set(False)
            self.status_var.set(f"启用{label}锁定失败：{exc}")

    def _schedule_resource_lock(self):
        if self._resource_lock_timer_id is not None:
            self.after_cancel(self._resource_lock_timer_id)
            self._resource_lock_timer_id = None
        if self._resource_lock_targets and not self._closing:
            self._resource_lock_timer_id = self.after(self._lock_interval(), self._resource_lock_loop)

    def _resource_lock_loop(self):
        self._resource_lock_timer_id = None
        if not self._resource_lock_targets or self._closing or not self._connected:
            return
        if self._busy is not None:
            self._schedule_resource_lock()
            return
        targets = dict(self._resource_lock_targets)
        # 锁定期间资源 entry 仍可编辑：用户编辑的值不再是“需要写回的差异”，而是新的目标（移动目标）。
        for key in list(targets):
            try:
                edited = self.parse_number(
                    self.resource_vars[key].get(), self._resource_labels[key],
                    RESOURCE_INPUT_MAX.get(key, RESOURCE_SAFE_MAX),
                )
            except ValueError:
                continue
            if edited != targets[key]:
                targets[key] = edited
        def run(game):
            data = game.read_resources()
            changes = {key: value for key, value in targets.items() if data.get(key) != value}
            if changes:
                game.write_resources(changes)
                data = game.read_resources()
            return data, targets
        def done(payload):
            data, targets = payload
            for key, value in targets.items():
                if key in self._resource_lock_targets:
                    self._resource_lock_targets[key] = value
            self._resource_lock_failures = 0
            if self.snapshot:
                self.snapshot.update(data)
            # 不回写输入框：锁定轮询每 700ms 一次，若把读回值写进 Entry，会覆盖用户
            # 正在输入的内容（包括未锁定的资源项），表现为输入被“自动刷新掉”。
            # Entry 始终保留用户的目标值；需要读取游戏当前值时使用“刷新数值”。
            self._update_resource_warning()
            self._schedule_resource_lock()
        def failed(error):
            self._resource_lock_failures += 1
            self.status_var.set(f"资源锁定检查失败（{self._resource_lock_failures}/3）：{error}")
            if self._resource_lock_failures >= 3:
                self._resource_lock_targets.clear()
                for var in self.resource_lock_vars.values():
                    var.set(False)
                self.status_var.set("资源锁定已停止：连续读取或写入失败，请进入游戏并刷新数值")
            else:
                self._schedule_resource_lock()
        self._submit("检查资源锁定", run, done, failed, quiet=True, background=True)

    def _stop_resource_lock(self):
        if self._resource_lock_timer_id is not None:
            self.after_cancel(self._resource_lock_timer_id)
            self._resource_lock_timer_id = None
        self._resource_lock_failures = 0

    def check_crew_param_limits(self, crew):
        for i in range(6):
            total = crew["params"][i] + crew["addParams"][i]
            limit = PARAM_SAFE_TOTAL if PARAM_NAMES[i] in PARAM_SAFE_LIMITED else PARAM_SUM_LIMIT
            if total > limit:
                if limit == PARAM_SAFE_TOTAL:
                    raise ValueError(
                        f"{PARAM_NAMES[i]} 的基础 {crew['params'][i]} 与成长 {crew['addParams'][i]} 合计 {total}，"
                        f"超过 {limit}；该项在探索/战斗计算中含 32 位乘法（属性相性或技能百分比），"
                        "超出后会导致打不动怪、被秒杀或恢复异常，请调低后再应用"
                    )
                raise ValueError(
                    f"{PARAM_NAMES[i]} 的基础 {crew['params'][i]} 与成长 {crew['addParams'][i]} "
                    f"合计 {total}，超过 {limit}；游戏按 32 位整数求和，溢出后该属性会归为 1，请调低后再应用"
                )

    def apply_crew_restore(self):
        if not self.snapshot or not self.snapshot["crews"]:
            messagebox.showerror("无法应用", "没有可编辑的船员，请先刷新", parent=self)
            return
        try:
            index = self.snapshot["crews"][self.crew_position]["index"]
            self._submit_write(
                "恢复战斗 HP/SP",
                write=lambda game: game.restore_crew_battle(index),
                read_back=lambda game: game.read_crew_battle(index),
                apply_readback=self._apply_crew_battle_readback,
                message="已按游戏当前计算上限恢复战斗 HP/SP",
            )
        except Exception as exc:
            self.status_var.set("恢复失败")
            messagebox.showerror("恢复失败", str(exc), parent=self)

    def apply_crew_max(self):
        if not self.snapshot or not self.snapshot["crews"]:
            messagebox.showerror("无法应用", "没有可编辑的船员，请先刷新", parent=self)
            return
        for var in self.param_vars:
            var.set(str(PARAM_ONECLICK_BASE))
        for var in self.add_param_vars:
            var.set(str(PARAM_ONECLICK_GROWTH))
        self.apply_crew()

    def apply_crew(self):
        if not self.snapshot or not self.snapshot["crews"]:
            messagebox.showerror("无法应用", "没有可编辑的船员，请先刷新", parent=self)
            return
        try:
            index = self.snapshot["crews"][self.crew_position]["index"]
            crew = {"index": index}
            for key, var in self.crew_vars.items():
                crew[key] = self.parse_number(var.get(), key)
            crew["params"] = [self.parse_number(var.get(), f"基础{PARAM_NAMES[i]}") for i, var in enumerate(self.param_vars)]
            crew["addParams"] = [self.parse_number(var.get(), f"成长{PARAM_NAMES[i]}") for i, var in enumerate(self.add_param_vars)]
            self.check_crew_param_limits(crew)
            self._submit_write(
                "船员应用",
                write=lambda game: game.update_crew(crew),
                read_back=lambda game: game.read_crew(index),
                apply_readback=self._apply_crew_readback,
                message=f"船员属性已写入并完成回读；备份目录：{self.settings.get('backup_root', DEFAULT_BACKUP_ROOT)}",
            )
        except Exception as exc:
            self.status_var.set("船员应用失败")
            messagebox.showerror("船员应用失败", str(exc), parent=self)

    def _apply_crew_battle_readback(self, data):
        if self.snapshot is None:
            return
        self.snapshot["crews"][data["index"]].update(data)
        for key in ("battleHP", "battleSP"):
            self.crew_vars[key].set(str(data[key]))

    def _apply_crew_readback(self, crew):
        if self.snapshot is None:
            return
        self.snapshot["crews"][crew["index"]] = crew
        self._update_crew_choices()
        self.load_crew_form()

    def on_close(self):
        if self._closing:
            return
        for timer in (self._column_save_timer_id, self._geometry_timer_id, self._geometry_enable_timer_id, self._auto_connect_timer_id):
            if timer is not None:
                self.after_cancel(timer)
        self._column_save_timer_id = None
        self._geometry_timer_id = None
        self._geometry_enable_timer_id = None
        self._auto_connect_timer_id = None
        self._capture_geometry()
        self._capture_columns()
        if not self._persist_settings():
            if not messagebox.askyesno("设置保存失败", "设置未能保存，仍然关闭修改器？", parent=self):
                return
        self._closing = True
        self._stop_lock_remaining()
        self.lock_remaining_var.set(False)
        self._stop_resource_lock()
        self._set_controls_busy(True)
        self.status_var.set("正在等待当前任务完成并安全断开游戏……")
        self.worker.jobs.put(None)


if __name__ == "__main__":
    TrainerApp().mainloop()