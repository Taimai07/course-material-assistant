#!/usr/bin/env python3
"""Native desktop application; no console, pairing-code entry or AI required."""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "server"))
from app import make_handler, PORT, LocalServer
from browser_rpc import BrowserBroker, BrowserClient
from collector import CourseCollector
from google_api import write_private
from projects import ProjectManager
from store import Store, state_dir
from version import VERSION
from file_ops import SAFE_OPEN_EXTENSIONS
from desktop_views import ClassroomView


def enable_dpi_awareness():
    """Call before creating Tk; the packaged manifest also declares PerMonitorV2."""
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()


def dpi_state(root):
    if os.name == "nt":
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        context = user32.GetThreadDpiAwarenessContext()
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        return {"awareness": user32.GetAwarenessFromDpiAwarenessContext(context), "dpi": user32.GetDpiForWindow(root.winfo_id()) or 96}
    return {"awareness": None, "dpi": round(root.winfo_fpixels("1i"))}


def chrome_open(url):
    candidates = [shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome")]
    if os.name == "nt":
        candidates = [str(Path(os.environ.get(key, "")) / "Google/Chrome/Application/chrome.exe") for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")] + candidates
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            subprocess.Popen([candidate, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    raise ValueError("没有找到 Chrome。请安装 Chrome，或在 Chrome 中手动打开这个地址：\n" + url)


def open_folder(path):
    if not Path(path).is_dir():
        raise ValueError("资料文件夹尚未生成或位置暂不可用")
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_autostart(enabled):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise ValueError("登录后自动启动由 Windows 安装版提供")
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "CourseMaterialAssistant", 0, winreg.REG_SZ, subprocess.list2cmdline([sys.executable, "--minimized"]))
        else:
            try:
                winreg.DeleteValue(key, "CourseMaterialAssistant")
            except FileNotFoundError:
                pass


class DesktopApp:
    def __init__(self, root, store=None, start_service=True):
        self.root, self.store = root, store or Store()
        self.services_enabled = start_service
        self.messages = queue.Queue()
        self.checked = set()
        self.tree_nodes = {}
        self.storage_moving = False
        self.storage_copying = False
        self.storage_cancel = threading.Event()
        self.settings_window = None
        self.background_jobs = 0
        self.job_lock = threading.Lock()
        self.update_busy = False
        self.update_cancel = threading.Event()
        self.update_info = None
        self.startup_update_checked = False
        self.update_status = tk.StringVar(root, value="尚未检查软件更新")
        self.storage_status = tk.StringVar(root, value="")
        self.update_progress = tk.DoubleVar(root, value=0)
        self.library_cache = {}
        self.detail_signature = None
        self.alive = True
        self.refresh_job = None
        self.message_job = None
        self.library_executor = ThreadPoolExecutor(max_workers=2)
        self.library_pending = set()
        self.broker = BrowserBroker(store=self.store)
        self.broker.confirm_download = self.confirm_large_download
        self.manager = ProjectManager(self.store, self.broker, CourseCollector(self.store, self.broker))
        self.manager.raise_callback = lambda: self.messages.put(self.show)
        retry_fixed_error = False
        for project in self.manager.settings["projects"]:
            if project["status"] == "running":
                project["status"] = "waiting"
            if project["status"] == "error" and "tab_id" in project.get("last_error", ""):
                project.update(status="waiting", last_error="", next_scan=0)
                retry_fixed_error = True
        if retry_fixed_error:
            self.manager._save()
        self.server = None
        self.extension_folder = self.store.control_root / "browser-extension"
        self.extension_folder.mkdir(exist_ok=True)
        for source in (ROOT / "extension").iterdir():
            if source.is_file():
                shutil.copy2(source, self.extension_folder / source.name)
        self._layout()
        if start_service:
            self.manager.maintenance = self.storage_blocked()
            token_path = self.store.control_root / "pairing.json"
            if not token_path.exists():
                write_private(token_path, {"token": secrets.token_urlsafe(32)})
            token = json.loads(token_path.read_text(encoding="utf-8"))["token"]
            try:
                self.server = LocalServer(("127.0.0.1", PORT), make_handler(self.store, token, self.broker, self.manager))
            except OSError:
                client = BrowserClient(self.store.control_root)
                try:
                    if client.post("/client/show", {}).get("ok"):
                        self.root.after(100, self.root.destroy)
                        return
                except ValueError:
                    pass
                messagebox.showerror("需要关闭旧版服务", "资料桥端口已被占用。请关闭旧版 Python 窗口，再打开图形版。", parent=root)
                self.root.after(100, self.root.destroy)
                return
            self.server.daemon_threads = True
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            threading.Thread(target=self.manager.run, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.root.iconify)
        self.refresh_job = self.root.after(500, self.refresh)
        self.message_job = self.root.after(100, self.process_messages)
        if start_service:
            if not self.store.storage_available:
                self.storage_status.set("资料磁盘暂不可用，请接回后重新检查保存位置。")
                self.root.after(700, lambda: self.preferences(1))
            elif self.manager.maintenance:
                self.root.after(700, self.first_storage)
            self.root.after(1600, self.startup_software_check)
        # The extension opens its helper in the Chrome profile where it is installed.
        # Launching Chrome here would use Chrome's last profile, possibly a different account.

    def _layout(self):
        root = self.root
        root.title("课题资料助手")
        self.dpi = dpi_state(root)
        self.scale = max(1, self.dpi["dpi"] / 96)
        root.tk.call("tk", "scaling", self.dpi["dpi"] / 72)
        px = lambda value: round(value*self.scale)
        width, height = min(px(1220), root.winfo_screenwidth()-40), min(px(820), root.winfo_screenheight()-80)
        root.geometry(f"{width}x{height}")
        root.minsize(min(px(840), width), min(px(570), height))
        root.configure(bg="#f5f8fd")
        from tkinter import font as tkfont
        families = {name.casefold(): name for name in tkfont.families(root)}
        family = next((families[name.casefold()] for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Bitmap Song", "SimSun") if name.casefold() in families), "TkDefaultFont")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=10)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f8fd")
        style.configure("Card.TFrame", background="white", relief="solid", borderwidth=1)
        style.configure("TLabel", background="#f5f8fd", foreground="#26354a", font=(family, 10))
        style.configure("Hint.TLabel", foreground="#5d6c80")
        style.configure("Title.TLabel", font=(family, 23, "bold"))
        style.configure("PageTitle.TLabel", font=(family, 19, "bold"), foreground="#174ea6")
        style.configure("Section.TLabel", font=(family, 12, "bold"))
        style.configure("TButton", padding=(px(9),px(6)), font=(family,10))
        style.configure("Card.TButton", background="white", foreground="#26354a", padding=px(7), anchor="w")
        style.configure("File.TButton", background="white", foreground="#1967d2", anchor="w")
        style.configure("Primary.TButton", background="#1967d2", foreground="white")
        shell = ttk.Frame(root, padding=px(20))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(3, weight=1)
        header = ttk.Frame(shell)
        header.grid(row=0,column=0,sticky="ew",pady=(0,12))
        ttk.Label(header,text="课题资料助手",style="Title.TLabel").pack(side="left")
        ttk.Button(header,text="设置",command=self.preferences).pack(side="right")
        ttk.Label(header,text="v"+VERSION,style="Hint.TLabel").pack(side="right",padx=12)
        self.connection = ttk.Label(shell,text="正在检查浏览器连接…",wraplength=width-px(50))
        self.connection.grid(row=1,column=0,sticky="ew",pady=(0,12))
        toolbar = ttk.Frame(shell)
        toolbar.grid(row=2,column=0,sticky="ew",pady=(0,14))
        ttk.Button(toolbar,text="添加课程",command=self.add_project).pack(side="left")
        ttk.Button(toolbar,text="同步当前课程／课题",style="Primary.TButton",command=self.sync_selected).pack(side="left",padx=px(6))
        ttk.Button(toolbar,text="导出勾选",command=self.export_checked).pack(side="left")
        ttk.Button(toolbar,text="同步至网盘",command=self.sync_cloud).pack(side="right")
        self.classroom = ClassroomView(shell,self)
        self.classroom.grid(row=3,column=0,sticky="nsew")
        footer = ttk.Frame(shell)
        footer.grid(row=4,column=0,sticky="ew",pady=(12,0))
        ttk.Button(footer,text="全选",command=lambda:self.check_all(True)).pack(side="left")
        ttk.Button(footer,text="清空勾选",command=lambda:self.check_all(False)).pack(side="left",padx=px(6))
        self.selection_label = ttk.Label(footer,text="已勾选 0 项",style="Hint.TLabel")
        self.selection_label.pack(side="left")
        ttk.Button(footer,text="退出助手",command=self.quit).pack(side="right")
        self.detail = ttk.Label(shell,text="左侧选择课程，右侧逐级进入主题、课题和附件。关闭窗口后可继续后台同步。",style="Hint.TLabel",wraplength=width-px(50))
        self.detail.grid(row=5,column=0,sticky="ew",pady=(10,0))

    def check_all(self, enabled):
        self.checked = {p["id"] for p in self.manager.snapshot()["projects"]} if enabled else set()
        self.update_checks()

    def update_checks(self):
        self.selection_label.configure(text=f"已勾选 {len(self.checked)} 项（课程包含子课题）")
        self.classroom.render(force=True)

    def export_checked(self):
        if not self.checked:
            messagebox.showinfo("先勾选资料", "请在列表的勾选列选择课程或课题。", parent=self.root)
            return
        target = filedialog.asksaveasfilename(parent=self.root, title="保存合并资料包", defaultextension=".zip", initialfile="课程课题资料包.zip", filetypes=[("ZIP 资料包", "*.zip")])
        if not target:
            return
        selected = list(self.checked)
        def work():
            try:
                result = self.manager.export_selected(selected, target)
                message = f"已导出 {result['projects']} 项资料到：\n{target}"
                if result.get("skipped"):
                    message += "\n\n以下项目尚未就绪，未包含在资料包中：\n" + "\n".join(p["name"] for p in result["skipped"])
                self.messages.put(lambda: self.notice("导出结果", message))
            except (ValueError, OSError) as error:
                message = str(error)
                self.messages.put(lambda: self.notice("导出未完成", message))
        self.start_material_work(work)

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def notice(self, title, message):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=min(round(600*self.scale), self.root.winfo_screenwidth()-100)).pack(fill="x")
        ttk.Button(frame, text="关闭", command=dialog.destroy).pack(anchor="e", pady=12)

    def guarded(self, fn):
        try:
            fn()
        except (ValueError, OSError) as error:
            messagebox.showerror("暂时无法完成", str(error), parent=self.root)

    def setup_browser(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("第一次设置 · 只做一次")
        dialog.geometry(f"{round(690*self.scale)}x{round(460*self.scale)}")
        frame = ttk.Frame(dialog, padding=25)
        frame.pack(fill="both", expand=True)
        text = "① 在登录学校账号的 Chrome 窗口打开 chrome://extensions，\n    开启「开发者模式」。\n\n② 点击「加载已解压的扩展程序」，选中下面的文件夹。\n\n③ 在这个 Chrome 的扩展菜单点击「课题资料桥」→「连接课题资料助手」。\n    从旧版更新：在扩展卡片点击一次「重新加载」。\n    首次允许访问课程后，以后无需输入配对码。"
        ttk.Label(frame, text=text, wraplength=round(590*self.scale)).pack(anchor="w", pady=15)
        ttk.Label(frame, text=str(self.extension_folder), wraplength=round(590*self.scale), style="Hint.TLabel").pack(anchor="w")
        ttk.Button(frame, text="复制文件夹路径", command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append(str(self.extension_folder)))).pack(side="left", pady=20)
        ttk.Button(frame, text="复制扩展页面地址", command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append("chrome://extensions"))).pack(side="right", pady=20)

    def connect_browser(self):
        self.manager.pair_until = time.time() + 300
        if self.broker.status()["connected"]:
            self.connection.configure(text="浏览器已连接 · 自动同步已就绪")
            return
        messagebox.showinfo("在学校账号所在的 Chrome 中连接", "请在已登录学校 Classroom 的 Chrome 窗口中，\n点击右上角扩展菜单 → 课题资料桥 → 连接课题资料助手。\n\n已设置的扩展会自动重连。这里已开启 5 分钟首次配对窗口。", parent=self.root)

    def add_project(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("添加课题")
        dialog.geometry(f"{min(round(760*self.scale), self.root.winfo_screenwidth()-40)}x{min(round(520*self.scale), self.root.winfo_screenheight()-90)}")
        frame = ttk.Frame(dialog, padding=25)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="从已打开的课程中选择（也可以在下方粘贴链接）").pack(anchor="w")
        choices = ttk.Combobox(frame, state="readonly")
        choices.pack(fill="x", pady=(4, 12))
        available = []
        ttk.Label(frame, text="课题名称").pack(anchor="w")
        name = ttk.Entry(frame)
        name.pack(fill="x", pady=(4, 15))
        ttk.Label(frame, text="课程或网盘文件夹链接（从浏览器地址栏复制）").pack(anchor="w")
        url = ttk.Entry(frame)
        url.pack(fill="x", pady=(4, 15))
        def select(_):
            index = choices.current()
            if 0 <= index < len(available):
                item = available[index]
                url.delete(0, "end")
                url.insert(0, item["url"])
                if not name.get():
                    name.insert(0, item["name"])
        choices.bind("<<ComboboxSelected>>", select)
        def discover():
            try:
                items = self.manager.discover()["items"]
                def apply():
                    if dialog.winfo_exists():
                        available.extend(items)
                        choices.configure(values=[item["name"] for item in items])
                self.messages.put(apply)
            except (ValueError, OSError):
                pass
        if self.broker.status()["connected"]:
            threading.Thread(target=discover, daemon=True).start()
        ttk.Label(frame, text="加入后每 15 分钟自动检查。资料保存在本机，不会自动提交给 AI。", wraplength=round(590*self.scale), style="Hint.TLabel").pack(anchor="w")
        def save():
            self.manager.add(name.get(), url.get().strip())
            dialog.destroy()
        ttk.Button(frame, text="添加并自动同步", command=lambda: self.guarded(save)).pack(anchor="e", pady=20)

    def selected(self):
        if not self.classroom.current_project_id:
            raise ValueError("请先选择一门课程或一个课题")
        return self.manager.project(self.classroom.current_project_id)

    def sync_selected(self):
        self.guarded(lambda: self.manager.request_sync(self.selected()["id"], force=True))

    def open_selected(self, bundle):
        def action():
            if self.manager.maintenance:
                raise ValueError("请先确认资料位置，或等待迁移／更新完成")
            self.store.require_available()
            project = self.selected()
            path = project.get("latest_bundle") if bundle else self.store.root / "projects" / project["id"] / "资料库"
            if not path:
                raise ValueError("第一次同步完成后就会生成资料包")
            open_folder(path)
        self.guarded(action)

    def library_view(self, project):
        if self.storage_moving or not self.store.storage_available:
            return {"project": project, "files": [], "pages": [], "children": [], "loading": True, "task": None}
        catalog = self.store.root / "projects" / project["id"] / "catalog.json"
        task = self.store.root / "collections" / (project.get("current_task", "none") + ".json")
        stamps = tuple(p.stat().st_mtime_ns if p.exists() else None for p in (catalog, task))
        selected_project = self.classroom.current_project_id
        include_text = selected_project == project["id"]
        key = (stamps, project.get("last_synced"), project.get("files"), tuple(project.get("material_ids", [])), include_text)
        with self.manager.lock:
            children = [dict(p) for p in self.manager.settings["projects"] if p.get("parent_id") == project["id"]]
        key += (tuple((p["id"], p["name"], p.get("topic"), p.get("files"), p.get("last_synced")) for p in children), int(time.monotonic() // 15))
        previous = self.library_cache.get(project["id"])
        if (not previous or previous[0] != key) and project["id"] not in self.library_pending:
            self.library_pending.add(project["id"])
            future = self.library_executor.submit(self.manager.library, project["id"], include_text=include_text)
            def done(result):
                try:
                    data = result.result()
                except Exception as error:
                    message = str(error)[:300]
                    def failed():
                        self.library_pending.discard(project["id"])
                        self.detail.configure(text="读取资料库暂时失败，将重试：" + message)
                    self.messages.put(failed)
                    return
                def apply():
                    self.library_pending.discard(project["id"])
                    self.library_cache[project["id"]] = (key, data)
                self.messages.put(apply)
            future.add_done_callback(done)
        if previous:
            return dict(previous[1], children=children, _view_revision=previous[0])
        return {"project": project, "files": [], "pages": [], "children": children, "loading": True, "task": None}

    def open_file(self, project_id, key):
        if self.manager.maintenance:
            raise ValueError("请等待资料迁移或软件更新完成后再打开附件")
        from library import local_file
        path = local_file(self.manager, project_id, key)
        if path.suffix.lower() not in SAFE_OPEN_EXTENSIONS:
            if not messagebox.askyesno("确认打开附件", "这个类型需要额外确认：" + path.name + "\n确认要打开吗？", parent=self.root):
                return
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def offline_classroom(self):
        def work():
            from library import write_offline_classroom
            try:
                path = write_offline_classroom(self.manager)
                def launch():
                    if os.name == "nt":
                        os.startfile(str(path))
                    else:
                        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
                self.messages.put(launch)
            except (OSError, ValueError) as error:
                message = str(error)
                self.messages.put(lambda: self.notice("无法生成本地课堂", message))
        self.start_material_work(work)

    def show_detail(self, force=False):
        self.classroom.render(force=force)

    def confirm_large_download(self, information):
        finished = threading.Event()
        expired = threading.Event()
        deadline = time.monotonic() + information.get("timeout_seconds", 300)
        answer = {"allowed": False, "remember": False}
        def cancelled():
            return self.manager.stopping.is_set() or bool(information.get("cancelled") and information["cancelled"].is_set())
        def ask():
            if expired.is_set() or not self.alive:
                finished.set()
                return
            amount = f"{information['bytes']/1024/1024:.1f} MiB" if information["bytes"] is not None else "完整大小未知，可能很大"
            warning = information.get("reason") == "google_scan_warning"
            self.show()
            dialog = tk.Toplevel(self.root)
            dialog.title("Google 无法扫描此文件" if warning else "是否下载这个大文件？")
            dialog.transient(self.root)
            frame = ttk.Frame(dialog, padding=20)
            frame.pack(fill="both", expand=True)
            explanation = "Google 提示文件过大，无法进行病毒扫描。是否仍要下载原文件？\n记住此文件的扫描警告选择，可在设置中清除。" if warning else f"询问阈值：{information['threshold_bytes']/1024/1024:g} MiB（不是上限）\n此附件大小不变时记住选择，可在设置中清除。"
            ttk.Label(frame, text=information["filename"] + "\n大小：" + amount + "\n\n" + explanation, wraplength=min(round(460*self.scale), self.root.winfo_screenwidth()-100)).pack(fill="x")
            countdown = ttk.Label(frame)
            countdown.pack(pady=12)
            def close(allowed=False, remember=True):
                answer["allowed"] = bool(allowed and time.monotonic() < deadline and not expired.is_set())
                answer["remember"] = bool(remember and time.monotonic() < deadline and not expired.is_set() and not cancelled())
                finished.set()
                dialog.destroy()
            ttk.Button(frame, text="下载", command=lambda: close(True)).pack(side="left", padx=8)
            ttk.Button(frame, text="跳过（记住选择）", command=close).pack(side="right", padx=8)
            dialog.protocol("WM_DELETE_WINDOW", lambda: close(False, False))
            def tick():
                if not dialog.winfo_exists():
                    return
                seconds = max(0, int(deadline - time.monotonic()))
                if expired.is_set() or not self.alive or seconds <= 0 or cancelled():
                    close(False, False)
                    return
                countdown.configure(text=f"{seconds} 秒内未选择将自动跳过")
                dialog.after(250, tick)
            tick()
        self.messages.put(ask)
        while not finished.wait(.2):
            if cancelled() or time.monotonic() >= deadline:
                expired.set()
                return {"allowed": False, "remember": False}
        return answer

    def preferences(self, tab=0):
        from desktop_settings import SettingsWindow
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.tabs.select(tab)
            return
        self.settings_window = SettingsWindow(self, tab=tab)

    def sync_cloud(self, verify=False):
        from library import PROVIDERS, sync_to_folder
        selected = list(self.checked)
        enabled = [name for name in PROVIDERS if self.manager.settings["cloud_folders"].get(name, {}).get("enabled")]
        if not selected or not enabled:
            messagebox.showinfo("先选择资料和网盘", "请勾选课程／课题，并在设置里启用要使用的网盘目录。", parent=self.root)
            return
        def work():
            try:
                reports = [sync_to_folder(self.manager, selected, name, verify=verify) for name in enabled]
                message = "\n".join(r["provider"] + "：" + str(r["projects"]) + " 项资料，跳过 " + str(len(r.get("skipped", []))) + " 项，失败 " + str(len(r.get("failed", []))) + " 项，保留冲突副本 " + str(len(r.get("conflicts", []))) + " 项" for r in reports) + "\n实际上传由网盘客户端完成。失败原因见项目状态。"
                self.messages.put(lambda: self.notice("已交给网盘同步", message))
            except (OSError, ValueError) as error:
                message = str(error)
                self.messages.put(lambda: self.notice("网盘同步未完成", message))
        self.start_material_work(work)

    def cloud_settings(self):
        self.preferences(tab=3)

    def startup(self):
        self.preferences(tab=0)

    def process_messages(self):
        if not self.alive:
            return
        if self.message_job is not None:
            self.root.after_cancel(self.message_job)
        # Schedule first: even a nested native dialog must not stop delivery of confirmations.
        self.message_job = self.root.after(100, self.process_messages)
        self._drain_messages()

    def _drain_messages(self):
        while True:
            try:
                callback = self.messages.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as error:
                if self.alive:
                    self.detail.configure(text="操作未完成：" + str(error)[:250])

    def refresh(self):
        if not self.alive:
            return
        if self.refresh_job is not None:
            self.root.after_cancel(self.refresh_job)
        try:
            self._refresh_view()
        except Exception as error:
            if self.alive:
                self.connection.configure(text="界面刷新暂时失败，将自动重试：" + str(error)[:250])
        finally:
            if self.alive:
                self.refresh_job = self.root.after(1500, self.refresh)

    def _refresh_view(self):
        snapshot = self.manager.snapshot()
        browser = "浏览器已连接" if snapshot["browser"]["connected"] else "等待浏览器连接 · 请完成首次设置并登录课程"
        cloud = {"connected": " · 聊天通道在线", "login_required": " · 聊天连接需要登录", "offline": " · 聊天通道离线"}.get(snapshot["client"]["cloud"], "")
        version = snapshot["client"].get("extension_version")
        if version and version != VERSION:
            browser += " · 扩展版本较旧，请在 chrome://extensions 重新加载"
        if snapshot["client"].get("discovering"):
            browser += " · 正在自动发现课程和课题…"
        elif snapshot["browser"]["connected"] and not snapshot["projects"]:
            browser += " · 请打开 Classroom 课程首页，30 秒内自动检查"
        active = snapshot["browser"].get("active")
        if active and active["operation"] in {"download", "download_url"}:
            phase = {"awaiting_confirmation": "等待下载确认", "importing": "正在归档原件", "downloading": "正在下载"}.get(active["phase"], "检查附件")
            browser += f" · {phase} · {(active.get('bytes') or 0)/1024/1024:.1f} MiB"
        cloud_errors = snapshot["client"].get("cloud_folder_errors", {})
        if cloud_errors:
            browser += " · 网盘有 " + str(len(cloud_errors)) + " 项未完成：" + next(iter(cloud_errors.values()))
        discovery = snapshot["client"].get("discovery_error", "")
        self.connection.configure(text=browser + cloud + (" · 自动发现待重试：" + discovery if discovery else ""))
        if self.storage_moving:
            self.connection.configure(text=self.storage_status.get())
            return
        if self.manager.maintenance:
            self.connection.configure(text=browser + " · " + (self.storage_status.get() or "请先确认资料保存位置，或等待软件更新完成"))
        self.classroom.render()

    def start_material_work(self, function):
        with self.manager.lock, self.job_lock:
            if self.manager.maintenance:
                self.notice("暂时无法开始", "请先确认保存位置，或等待迁移／软件更新完成。")
                return
            self.background_jobs += 1
        def run():
            try:
                function()
            finally:
                with self.job_lock:
                    self.background_jobs -= 1
        threading.Thread(target=run,daemon=True).start()

    def wait_material_idle(self, cancelled=None, timeout=600):
        deadline=time.monotonic()+timeout
        while self.alive and not self.manager.stopping.is_set():
            with self.job_lock:
                busy = self.background_jobs > 0
            with self.manager.collector.lock:
                collecting = any(t["status"] == "running" for t in self.manager.collector.tasks.values())
            if not busy and not collecting and not self.manager.active and not self.manager.client_state.get("discovering") and not self.broker.status()["pending_commands"]:
                return True
            if cancelled and cancelled():
                return False
            if time.monotonic()>=deadline:
                raise ValueError("等待资料任务完成已超时，请稍后重试")
            time.sleep(.2)
        return False

    def storage_blocked(self):
        from storage import storage_available
        self.store.storage_available = storage_available(self.store.control_root, self.store.root)
        return not self.store.storage_available or not (self.store.control_root / "storage-location.json").exists()

    def recheck_storage(self):
        if self.storage_moving or self.update_busy:
            raise ValueError("请等待迁移或软件更新完成")
        self.store.require_available()
        with self.manager.lock:
            self.manager.collector = CourseCollector(self.store, self.broker)
            self.manager.maintenance = self.storage_blocked()
            self.manager.next_discovery = 0
        self.library_cache.clear()
        self.classroom.signature = None
        self.storage_status.set("保存位置已连接：" + str(self.store.root))
        self.notice("保存位置可用", str(self.store.root))
        self.startup_software_check()

    def first_storage(self):
        if (self.store.control_root / "storage-location.json").exists():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("先选择资料保存位置")
        frame = ttk.Frame(dialog,padding=22)
        frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="课程附件可能占用较多空间，请在开始下载前确认保存位置。",wraplength=round(550*self.scale)).pack(anchor="w",pady=10)
        ttk.Label(frame,text=str(self.store.root),wraplength=round(550*self.scale),style="Hint.TLabel").pack(anchor="w",pady=10)
        def current():
            dialog.destroy()
            self.change_storage(self.store.root,remove_old=False)
        def choose():
            target=filedialog.askdirectory(parent=dialog,title="选择空文件夹保存课题资料")
            if target:
                dialog.destroy()
                self.change_storage(Path(target),remove_old=True)
        ttk.Button(frame,text="选择其他位置",command=choose).pack(side="left",pady=14)
        ttk.Button(frame,text="使用此位置",command=current).pack(side="right",pady=14)

    def cancel_storage_wait(self):
        if self.storage_copying:
            self.notice("正在复制资料", "为保证资料完整，请等待复制完成。")
        else:
            self.storage_cancel.set()

    def change_storage(self, target, remove_old=True):
        from storage import validate_destination, migrate
        if self.storage_moving or self.update_busy:
            self.notice("请稍候","已有迁移或软件更新在进行。")
            return
        try:
            self.store.require_available()
            target=validate_destination(self.store,target)
        except (ValueError,OSError) as error:
            self.notice("不能使用这个位置",str(error))
            return
        with self.manager.lock, self.job_lock:
            self.storage_cancel.clear()
            self.storage_moving=True
            self.manager.maintenance=True
        self.storage_status.set("正在等待当前资料操作完成…")
        def work():
            report=None
            message=None
            try:
                if not self.wait_material_idle(cancelled=self.storage_cancel.is_set):
                    raise ValueError("已取消等待，资料保存位置未更改")
                with self.job_lock:
                    if not self.alive or self.storage_cancel.is_set():
                        return
                    self.storage_copying=True
                self.library_executor.shutdown(wait=True,cancel_futures=True)
                report=migrate(self.store,target,progress=lambda text:self.messages.put(lambda text=text:self.storage_status.set(text)),remove_old=remove_old)
            except Exception as error:
                message=str(error)
            finally:
                blocked=False
                try:
                    from storage import recover
                    recover(self.store.control_root)
                    with self.manager.lock:
                        self.manager.settings=json.loads(self.manager.path.read_text(encoding="utf-8")) if self.manager.path.exists() else self.manager.settings
                        self.manager.normalize()
                        self.manager._save()
                        self.manager.collector=CourseCollector(self.store,self.broker)
                except Exception as error:
                    blocked=True
                    message="迁移设置尚未恢复，请检查配置盘空间并重新打开助手："+str(error)
                def finish():
                    self.library_executor=ThreadPoolExecutor(max_workers=2)
                    self.library_cache.clear()
                    self.library_pending.clear()
                    self.storage_moving=False
                    self.storage_copying=False
                    self.manager.maintenance=blocked or self.storage_blocked()
                    self.classroom.signature=None
                    if report:
                        self.storage_status.set("资料保存位置："+str(self.store.root))
                        text="后续课程附件将保存到：\n"+str(self.store.root)
                        if report["leftovers"]:
                            text+="\n新位置已可用，但部分旧副本未能清理：\n"+"\n".join(report["leftovers"])
                        elif not remove_old and report["moved"]:
                            text+="\n按你的选择，旧位置的资料副本仍保留。"
                        self.notice("保存位置已更新",text)
                        self.root.after(400, self.startup_software_check)
                    elif message:
                        self.storage_status.set("位置更改未完成："+message)
                        self.notice("位置更改未完成",message+"\n当前使用："+str(self.store.root))
                    if self.settings_window is not None and self.settings_window.winfo_exists():
                        self.settings_window.location.configure(text=str(self.store.root))
                if self.alive:
                    self.messages.put(finish)
        threading.Thread(target=work,daemon=True).start()

    def startup_software_check(self):
        if not self.services_enabled or not self.alive or self.startup_update_checked or not self.manager.settings.get("check_software_updates", True):
            return
        if self.storage_moving or self.manager.maintenance:
            return
        self.startup_update_checked = True
        self.check_software_update()

    def check_software_update(self, manual=False):
        if self.update_busy:
            if manual:self.notice("软件更新","已有更新检查或下载在进行。")
            return
        from updates import check
        self.update_busy=True
        self.update_status.set("正在联网检查软件更新…")
        def work():
            try:
                info=check(VERSION)
                def result():
                    self.update_busy=False
                    self.update_info=info
                    self.update_status.set("发现新版本："+info["version"] if info else "当前已是最新版本（"+VERSION+"）")
                    if info:
                        self.offer_update(info)
                    elif manual:
                        self.notice("软件更新","当前已是最新版本（"+VERSION+"）。")
                self.messages.put(result)
            except Exception as error:
                message=str(error)
                def failed():
                    self.update_busy=False
                    self.update_status.set(message)
                    if manual:self.notice("检查更新未完成",message)
                self.messages.put(failed)
        threading.Thread(target=work,daemon=True).start()

    def offer_update(self, info):
        dialog=tk.Toplevel(self.root)
        dialog.title("发现新版本 "+info["version"])
        frame=ttk.Frame(dialog,padding=22)
        frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="发现课题资料助手 "+info["version"]+"，是否下载并打开安装程序？",wraplength=round(560*self.scale)).pack(anchor="w",pady=10)
        ttk.Label(frame,text=info.get("notes","")[:1500],wraplength=round(560*self.scale),style="Hint.TLabel").pack(anchor="w",pady=10)
        ttk.Label(frame,text=f"安装包约 {info['size']/1024/1024:.1f} MiB。下载完成后等待当前资料操作结束，再打开安装向导；资料和设置保留。",wraplength=round(560*self.scale)).pack(anchor="w",pady=10)
        def yes():
            dialog.destroy()
            self.download_software_update(info)
        ttk.Button(frame,text="稍后",command=dialog.destroy).pack(side="left",pady=12)
        ttk.Button(frame,text="下载并安装",style="Primary.TButton",command=yes).pack(side="right",pady=12)

    def download_software_update(self, info):
        if self.update_busy or self.storage_moving:
            self.notice("请稍后更新","已有迁移或软件更新任务在进行。")
            return
        from updates import download,launch
        self.update_busy=True
        self.update_cancel.clear()
        self.update_progress.set(0)
        self.update_status.set("正在下载软件更新…")
        def work():
            paused=False
            try:
                def progress(done,total):
                    value=done/total*100
                    self.messages.put(lambda:self.update_progress.set(value))
                path=download(info,self.store.control_root,progress=progress,cancelled=self.update_cancel.is_set)
                if self.update_cancel.is_set():
                    raise ValueError("已取消更新")
                with self.manager.lock, self.job_lock:
                    self.manager.maintenance=True
                paused=True
                self.messages.put(lambda:self.update_status.set("安装包已校验，等待当前资料操作完成…"))
                if not self.wait_material_idle(cancelled=self.update_cancel.is_set):
                    raise ValueError("已取消更新")
                def install():
                    try:
                        if self.update_cancel.is_set():
                            raise ValueError("已取消更新")
                        launch(path,info,self.store.control_root)
                        self.update_status.set("已打开安装程序")
                        self.root.after(500,self.quit)
                    except (ValueError,OSError) as error:
                        self.update_busy=False
                        self.manager.maintenance=self.storage_blocked()
                        self.update_status.set(str(error))
                        self.notice("未打开安装程序",str(error))
                self.messages.put(install)
            except Exception as error:
                message=str(error)
                def failed():
                    self.update_busy=False
                    if paused:self.manager.maintenance=self.storage_blocked()
                    self.update_status.set(message)
                    self.notice("软件更新未完成",message)
                self.messages.put(failed)
        threading.Thread(target=work,daemon=True).start()

    def quit(self):
        with self.job_lock:
            if self.storage_copying:
                self.notice("正在迁移资料", "请等待迁移完成再退出助手。")
                return
            self.storage_cancel.set()
            self.alive = False
        self.update_cancel.set()
        self.library_executor.shutdown(wait=False, cancel_futures=True)
        self.manager.stopping.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--self-check", type=Path)
    args = parser.parse_args()
    enable_dpi_awareness()
    root = tk.Tk()
    if args.self_check:
        root.withdraw()
        result = {"ok": True, "version": VERSION, "dpi": dpi_state(root), "tk": root.tk.call("info", "patchlevel"), "extension_assets": (ROOT / "extension/manifest.json").is_file()}
        args.self_check.write_text(json.dumps(result), encoding="utf-8")
        root.destroy()
        return
    try:
        # Show the live instance before opening the database or touching extension/settings files.
        try:
            if BrowserClient(state_dir()).post("/client/show", {}, timeout=2).get("ok"):
                root.destroy()
                return
        except (ValueError, OSError):
            pass
        app = DesktopApp(root)
    except (ValueError, OSError) as error:
        messagebox.showerror("无法打开资料助手", str(error), parent=root)
        root.destroy()
        return 1
    if args.minimized:
        root.after(200, root.iconify)
    root.mainloop()


if __name__ == "__main__":
    main()
