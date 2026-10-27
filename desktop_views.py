"""Native Classroom-style navigation: course sidebar and a separate drill-down content page."""
import tkinter as tk
from tkinter import ttk
from pathlib import Path
import time


class ClassroomView(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.route = ("home",)
        self.history = []
        self.current_project_id = None
        self.signature = None
        self.cards = {}
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(self, padding=(0, 0, 14, 0))
        sidebar.grid(row=0, column=0, sticky="nsew")
        ttk.Label(sidebar, text="我的课程", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        self.sidebar = tk.Listbox(sidebar, width=27, borderwidth=0, highlightthickness=0, exportselection=False,
                                  bg="#f5f8fd", fg="#26354a", selectbackground="#d3e3fd", selectforeground="#174ea6", activestyle="none")
        self.sidebar.pack(fill="both", expand=True)
        self.sidebar.bind("<ButtonRelease-1>", self.choose_course)
        self.sidebar.bind("<Return>", self.choose_course)
        self.sidebar.bind("<KeyRelease-Up>", self.choose_course)
        self.sidebar.bind("<KeyRelease-Down>", self.choose_course)
        self.course_ids = []
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew")
        navigation = ttk.Frame(right)
        navigation.pack(fill="x", pady=(0, 12))
        self.back_button = ttk.Button(navigation, text="← 返回", command=self.back)
        self.back_button.pack(side="left")
        self.breadcrumb = ttk.Label(navigation, text="所有课程", style="Hint.TLabel")
        self.breadcrumb.pack(side="left", padx=10)
        self.canvas = tk.Canvas(right, background="#f8fafd", highlightthickness=0)
        scroll = ttk.Scrollbar(right, orient="vertical", command=self.canvas.yview)
        scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=16)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.body.bind("<MouseWheel>", self.wheel)
        self.bind_all("<MouseWheel>", self.content_wheel, add="+")

    def wheel(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def content_wheel(self, event):
        if isinstance(event.widget, tk.Text):
            return
        widget = event.widget
        while widget is not None:
            if widget is self.body:
                return self.wheel(event)
            widget = getattr(widget, "master", None)

    def choose_course(self, _=None):
        selected = self.sidebar.curselection()
        if selected:
            self.navigate(("course", self.course_ids[selected[0]]))

    def navigate(self, route):
        if self.route != route:
            self.history.append(self.route)
        self.route = route
        self.current_project_id = route[1] if len(route) > 1 else None
        self.signature = None
        self.render(force=True)
        self.canvas.yview_moveto(0)

    def back(self):
        self.route = self.history.pop() if self.history else ("home",)
        self.current_project_id = self.route[1] if len(self.route) > 1 else None
        self.signature = None
        self.render(force=True)
        self.canvas.yview_moveto(0)

    def toggle(self, project_id):
        if project_id in self.app.checked:
            self.app.checked.remove(project_id)
        else:
            self.app.checked.add(project_id)
        self.app.update_checks()

    def label(self, text, hint=False):
        widget = ttk.Label(self.body, text=text, wraplength=max(240, self.canvas.winfo_width()-70),
                           style="Hint.TLabel" if hint else "TLabel", justify="left")
        widget.pack(anchor="w", fill="x", pady=(0, 10))
        return widget

    def card(self, text, command, project_id=None, subtitle="", key=None):
        frame = ttk.Frame(self.body, style="Card.TFrame", padding=14)
        frame.pack(fill="x", pady=(0, 10))
        frame.columnconfigure(1, weight=1)
        if project_id:
            variable = tk.BooleanVar(value=project_id in self.app.checked)
            check = ttk.Checkbutton(frame, variable=variable, command=lambda: self.toggle(project_id))
            check.variable = variable
            check.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        from tkinter import font as tkfont
        font = tkfont.nametofont("TkDefaultFont")
        width = max(220, self.canvas.winfo_width()-160)
        lines, line = [], ""
        for character in text:
            if character == "\n" or font.measure(line+character) > width:
                lines.append(line)
                line = "" if character == "\n" else character
            else:
                line += character
        lines.append(line)
        button = ttk.Button(frame, text="\n".join(lines), command=command, style="Card.TButton")
        button.grid(row=0, column=1, sticky="ew")
        if subtitle:
            ttk.Label(frame, text=subtitle, style="Hint.TLabel", wraplength=max(220, self.canvas.winfo_width()-110)).grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.cards[key or project_id or text] = button

    def render(self, force=False):
        projects = self.app.manager.snapshot()["projects"]
        by_id = {p["id"]: p for p in projects}
        courses = [p for p in projects if not p.get("parent_id")]
        names = [p["name"].replace("\n", " · ") for p in courses]
        ids = [p["id"] for p in courses]
        if ids != self.course_ids or list(self.sidebar.get(0, "end")) != names:
            self.sidebar.delete(0, "end")
            for name in names:
                self.sidebar.insert("end", name)
            self.course_ids = ids
        project = by_id.get(self.current_project_id)
        if self.current_project_id and not project:
            self.route, self.current_project_id = ("home",), None
        data = self.app.library_view(project) if project else None
        signature = (self.route, tuple((p["id"], p["name"], p.get("topic"), p["status"], p["files"], p.get("last_error"), p.get("last_synced")) for p in projects),
                     (data or {}).get("_view_revision"), tuple(sorted(self.app.checked)))
        if signature == self.signature and not force:
            return
        self.signature = signature
        position = self.canvas.yview()[0]
        for child in self.body.winfo_children():
            child.destroy()
        self.cards = {}
        self.back_button.configure(state="normal" if self.history else "disabled")
        if project:
            course_id = project.get("parent_id") or project["id"]
            if course_id in self.course_ids:
                self.sidebar.selection_clear(0, "end")
                self.sidebar.selection_set(self.course_ids.index(course_id))
        if self.route[0] == "home":
            self.breadcrumb.configure(text="所有课程")
            ttk.Label(self.body, text="我的课程", style="PageTitle.TLabel").pack(anchor="w", pady=(0, 18))
            self.label("从左侧选择课程，或点击下面的课程卡片。", True)
            for course in courses:
                self.card(course["name"], lambda pid=course["id"]: self.navigate(("course", pid)), course["id"], self.project_status(course))
            if not courses:
                self.label("连接浏览器并打开 Classroom 后会发现课程。首次使用请在设置中选择资料保存位置。", True)
        elif self.route[0] in {"course", "topic"}:
            topic = self.route[2] if self.route[0] == "topic" else None
            self.breadcrumb.configure(text=project["name"].replace("\n", " · ") + ("  /  " + topic if topic else ""))
            ttk.Label(self.body, text=topic or project["name"], style="PageTitle.TLabel", wraplength=max(250, self.canvas.winfo_width()-70)).pack(anchor="w", pady=(0, 16))
            self.project_diagnostics(project, data)
            children = [p for p in projects if p.get("parent_id") == project["id"]]
            if topic is None:
                for name in dict.fromkeys(p.get("topic") or "未分组" for p in children):
                    count = sum((p.get("topic") or "未分组") == name for p in children)
                    self.card(name, lambda name=name: self.navigate(("topic", project["id"], name)), subtitle=f"{count} 个课题 · 点击进入", key="topic:" + name)
                if data and (data["pages"] or data["files"]):
                    self.card("课程说明与附件", lambda: self.navigate(("material", project["id"])), subtitle="查看已采集的课程资料", key="course-material")
                if not children:
                    self.label("尚未发现课题。可点击“同步当前课程”，或查看上方的具体失败原因。", True)
            else:
                for child in children:
                    if (child.get("topic") or "未分组") == topic:
                        self.card(child["name"], lambda pid=child["id"]: self.navigate(("assignment", pid)), child["id"], self.project_status(child))
        else:
            parent = by_id.get(project.get("parent_id"), project)
            self.breadcrumb.configure(text="  /  ".join(dict.fromkeys([parent["name"].split("\n")[0], project.get("topic") or "课题资料", project["name"].split("\n")[0]])))
            ttk.Label(self.body, text=project["name"], style="PageTitle.TLabel", wraplength=max(250, self.canvas.winfo_width()-70)).pack(anchor="w", pady=(0, 16))
            self.project_diagnostics(project, data)
            for page in data["pages"]:
                self.label(page["title"])
                text = tk.Text(self.body, wrap="word", height=min(14, max(4, len(page["text"])//90)), relief="flat", bg="white", font="TkTextFont", padx=14, pady=12)
                text.insert("1.0", page["text"])
                text.configure(state="disabled")
                text.pack(fill="x", pady=(0, 12))
                if page.get("preview_truncated"):
                    self.label("此处为正文预览，完整内容保留在导出资料包。", True)
            ttk.Label(self.body, text="课件与附件", style="Section.TLabel").pack(anchor="w", pady=(12, 12))
            for file in data["files"]:
                size = f" · {file['bytes']/1024/1024:.1f} MiB" if file.get("bytes") else ""
                if file.get("path"):
                    button = ttk.Button(self.body, text=file["name"] + size, style="File.TButton", command=lambda f=file: self.app.guarded(lambda: self.app.open_file(project["id"], f["key"])))
                    button.pack(anchor="w", fill="x", pady=(0, 6))
                    self.cards["file:" + file["key"]] = button
                    for message in [file.get("update_error"), file.get("freshness_warning"), file.get("error")] + file.get("warnings", []):
                        if message:
                            self.label(message, True)
                else:
                    self.label(file["name"] + " · " + (file.get("error") or "等待下载"), True)
            if not data["files"]:
                self.label("正在读取本地资料…" if data.get("loading") else "尚未发现或下载附件，请查看同步状态。", True)
        if not force:
            self.canvas.yview_moveto(position)

    def project_status(self, project):
        names = {"waiting":"等待首次同步", "running":"正在读取与下载", "ready":"已同步", "gaps":"部分资料待处理", "error":"同步失败"}
        return names.get(project["status"], project["status"]) + f" · {project['files']} 个本地附件" + (" · " + project.get("last_error", "") if project.get("last_error") else "")

    def project_diagnostics(self, project, data):
        self.label(self.project_status(project), True)
        task = (data or {}).get("task")
        if task:
            self.label(f"本次发现 {task.get('files_discovered') or 0} 个附件；未访问页面 {task.get('unvisited_pages') or 0}；超出数量设置 {task.get('undownloaded_due_to_limit') or 0}", True)
            for error in task.get("errors") or []:
                self.label("未完成：" + str(error.get("error", "")), True)
        for provider, error in project.get("cloud_errors", {}).items():
            self.label(provider + "：" + error, True)
