"""All preferences in a single native tabbed settings page."""
import json
import os
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk, filedialog
import webbrowser

from library import PROVIDERS
from version import VERSION
from updates import ORIGIN


class SettingsWindow(tk.Toplevel):
    def __init__(self, app, tab=0):
        super().__init__(app.root)
        self.app = app
        self.title("设置 · 课题资料助手")
        self.geometry(f"{min(round(860*app.scale), self.winfo_screenwidth()-50)}x{min(round(710*app.scale), self.winfo_screenheight()-90)}")
        self.minsize(min(650,self.winfo_screenwidth()-50),min(500,self.winfo_screenheight()-90))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both",expand=True,padx=16,pady=16)
        self.frames = []
        for title in ("常规", "资料与下载", "网盘同步", "浏览器与聊天", "关于与更新"):
            page=ttk.Frame(self.tabs)
            self.tabs.add(page,text=title)
            canvas=tk.Canvas(page,highlightthickness=0,bg="#f5f8fd")
            scroll=ttk.Scrollbar(page,orient="vertical",command=canvas.yview)
            scroll.pack(side="right",fill="y")
            canvas.configure(yscrollcommand=scroll.set)
            canvas.pack(fill="both",expand=True)
            frame=ttk.Frame(canvas,padding=20)
            window=canvas.create_window(0,0,anchor="nw",window=frame)
            frame.bind("<Configure>",lambda _,c=canvas:c.configure(scrollregion=c.bbox("all")))
            canvas.bind("<Configure>",lambda e,c=canvas,w=window:c.itemconfigure(w,width=e.width))
            frame.columnconfigure(1,weight=1)
            self.frames.append(frame)
        settings=app.manager.settings
        general=self.frames[0]
        self.heading(general,"常规设置")
        self.automatic=tk.BooleanVar(value=settings["automatic"])
        self.discover=tk.BooleanVar(value=settings["auto_discover"])
        self.updates=tk.BooleanVar(value=settings.get("check_software_updates",True))
        for row,text,value in ((1,"自动同步课程资料",self.automatic),(2,"自动发现已打开的课程与课题",self.discover),(3,"打开应用时联网检查软件更新（发现新版后询问）",self.updates)):
            ttk.Checkbutton(general,text=text,variable=value).grid(row=row,column=0,columnspan=3,sticky="w",pady=10)
        self.autostart_available=os.name=="nt" and getattr(sys,"frozen",False)
        self.initial_autostart=False
        if self.autostart_available:
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                    winreg.QueryValueEx(key,"CourseMaterialAssistant")
                    self.initial_autostart=True
            except FileNotFoundError:
                pass
        self.autostart=tk.BooleanVar(value=self.initial_autostart)
        ttk.Checkbutton(general,text="登录 Windows 后自动启动",variable=self.autostart,state="normal" if self.autostart_available else "disabled").grid(row=4,column=0,columnspan=3,sticky="w",pady=10)
        self.note(general,"关闭主窗口会最小化，继续后台同步；点击主窗口的“退出助手”会停止运行。",5)
        data=self.frames[1]
        self.heading(data,"资料保存位置")
        self.location=ttk.Label(data,text=str(app.store.root),wraplength=round(640*app.scale))
        self.location.grid(row=1,column=0,columnspan=3,sticky="ew",pady=8)
        self.remove_old=tk.BooleanVar(value=True)
        ttk.Checkbutton(data,text="迁移校验成功后，清理旧位置的资料副本",variable=self.remove_old).grid(row=2,column=0,columnspan=3,sticky="w",pady=8)
        self.move_button=ttk.Button(data,text="选择新位置并迁移已有资料",command=self.choose_storage)
        self.move_button.grid(row=3,column=0,sticky="w",pady=8)
        ttk.Button(data,text="打开资料文件夹",command=lambda:app.guarded(lambda:__import__('desktop').open_folder(app.store.root))).grid(row=3,column=2,sticky="e")
        ttk.Button(data,text="取消等待迁移",command=app.cancel_storage_wait).grid(row=3,column=1,sticky="e",padx=8)
        ttk.Label(data,textvariable=app.storage_status,wraplength=round(640*app.scale)).grid(row=4,column=0,columnspan=3,sticky="ew",pady=8)
        self.note(data,"课程原件、正文索引和资料包保存在这里；登录配对与浏览器扩展留在原配置位置。新位置需要是空文件夹。",5)
        if not (app.store.control_root/"storage-location.json").exists():
            ttk.Button(data,text="确认继续使用当前位置",command=lambda:app.change_storage(app.store.root,remove_old=False)).grid(row=6,column=0,columnspan=3,sticky="w",pady=8)
        ttk.Button(data,text="重新检查保存位置",command=lambda:app.guarded(app.recheck_storage)).grid(row=6,column=2,sticky="e",pady=8)
        self.number(data,"超过此大小先询问（MiB，不是下载上限）",7,"threshold",settings["large_file_mib"],1,65536)
        self.number(data,"每轮最多处理的附件数量",8,"maximum",settings["scan_max_files"],1,10000)
        self.number(data,"大文件无法识别更新时，完整复查间隔（小时）",9,"recheck",settings["unvalidated_recheck_hours"],0,168)
        self.note(data,"复查间隔为 0 表示每轮重新检查；主界面的“同步当前课程／课题”会立即复查。",10)
        ttk.Button(data,text="清除记住的大文件选择",command=self.clear_choices).grid(row=11,column=0,columnspan=3,sticky="w",pady=12)
        cloud=self.frames[2]
        self.heading(cloud,"网盘同步")
        self.note(cloud,"选择已安装网盘客户端的同步文件夹。程序只写入该目录，实际上传由网盘客户端完成。",1)
        self.providers={}
        for row,name in enumerate(PROVIDERS,2):
            previous=settings["cloud_folders"].get(name,{})
            enabled=tk.BooleanVar(value=previous.get("enabled",False))
            ttk.Checkbutton(cloud,text=name,variable=enabled).grid(row=row,column=0,sticky="w",pady=12)
            path=ttk.Entry(cloud)
            path.insert(0,previous.get("path",""));path.grid(row=row,column=1,sticky="ew",padx=8)
            def choose(entry=path):
                result=filedialog.askdirectory(parent=self,title="选择官方网盘客户端同步文件夹")
                if result:entry.delete(0,"end");entry.insert(0,result)
            ttk.Button(cloud,text="选择目录",command=choose).grid(row=row,column=2)
            self.providers[name]=(enabled,path)
        self.cloud_auto=tk.BooleanVar(value=settings.get("cloud_automatic",False))
        ttk.Checkbutton(cloud,text="每次资料同步完成后，自动写入已启用的网盘目录",variable=self.cloud_auto).grid(row=5,column=0,columnspan=3,sticky="w",pady=18)
        ttk.Button(cloud,text="完整校验主窗口已勾选项目的网盘副本",command=lambda:app.sync_cloud(verify=True)).grid(row=6,column=0,columnspan=3,sticky="w",pady=10)
        browser=self.frames[3]
        self.heading(browser,"浏览器连接")
        self.note(browser,"在已登录学校账号的 Chrome 中安装扩展并打开连接页。不要把配对码或 Google 登录信息发给他人。",1)
        ttk.Button(browser,text="首次设置浏览器",command=app.setup_browser).grid(row=2,column=0,sticky="w",pady=10)
        ttk.Button(browser,text="连接浏览器／查看连接方法",command=app.connect_browser).grid(row=2,column=1,columnspan=2,sticky="w",padx=10)
        self.note(browser,"可选：连接我的课题聊天。普通资料采集不需要 AI 或此连接。",3)
        self.relay=ttk.Entry(browser)
        preset=Path(__file__).resolve().parent/"personal-connection.json"
        if getattr(sys,"frozen",False):preset=Path(sys._MEIPASS)/"personal-connection.json"
        default=json.loads(preset.read_text(encoding="utf-8-sig")).get("relay_url","") if preset.exists() else ""
        self.relay.insert(0,settings.get("relay_url") or default)
        self.relay.grid(row=4,column=0,columnspan=3,sticky="ew",pady=10)
        ttk.Button(browser,text="保存聊天地址并打开登录",command=lambda:app.guarded(self.save_relay)).grid(row=5,column=0,columnspan=3,sticky="w")
        about=self.frames[4]
        self.heading(about,"关于与软件更新")
        self.note(about,"当前版本："+VERSION,1)
        ttk.Label(about,textvariable=app.update_status,wraplength=round(640*app.scale)).grid(row=2,column=0,columnspan=3,sticky="ew",pady=12)
        ttk.Progressbar(about,variable=app.update_progress,maximum=100).grid(row=3,column=0,columnspan=3,sticky="ew",pady=10)
        ttk.Button(about,text="检查软件更新",command=lambda:app.check_software_update(manual=True)).grid(row=4,column=0,sticky="w",pady=10)
        ttk.Button(about,text="取消下载",command=app.update_cancel.set).grid(row=4,column=1,sticky="w")
        ttk.Button(about,text="打开安装包下载页",command=lambda:webbrowser.open(ORIGIN)).grid(row=5,column=0,columnspan=3,sticky="w",pady=10)
        self.note(about,"发现新版本会先询问，确认后下载、校验并打开安装向导，保留课程、资料和设置。检查版本不会上传课程内容。",6)
        footer=ttk.Frame(self,padding=(16,0,16,16));footer.pack(fill="x")
        ttk.Button(footer,text="关闭",command=self.destroy).pack(side="right")
        ttk.Button(footer,text="保存设置",style="Primary.TButton",command=lambda:app.guarded(self.save)).pack(side="right",padx=10)
        self.tabs.select(tab)

    def heading(self,frame,text):
        ttk.Label(frame,text=text,style="Section.TLabel").grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,12))

    def note(self,frame,text,row):
        ttk.Label(frame,text=text,style="Hint.TLabel",wraplength=round(640*self.app.scale),justify="left").grid(row=row,column=0,columnspan=3,sticky="ew",pady=10)

    def number(self,frame,label,row,name,value,low,high):
        ttk.Label(frame,text=label,wraplength=round(470*self.app.scale)).grid(row=row,column=0,columnspan=2,sticky="w",pady=10)
        field=ttk.Spinbox(frame,from_=low,to=high,width=9)
        field.insert(0,str(value));field.grid(row=row,column=2,sticky="e")
        setattr(self,name,field)

    def choose_storage(self):
        target=filedialog.askdirectory(parent=self,title="选择空文件夹作为课题资料保存位置")
        if target:
            self.app.change_storage(Path(target),remove_old=self.remove_old.get())

    def save(self):
        if self.app.storage_moving:
            raise ValueError("资料迁移完成后再保存其他设置")
        try:threshold,maximum,recheck=int(self.threshold.get()),int(self.maximum.get()),int(self.recheck.get())
        except ValueError:raise ValueError("大小、数量和复查间隔需要填写整数") from None
        manager=self.app.manager
        manager.configure(threshold,maximum,{name:{"enabled":v.get(),"path":p.get()} for name,(v,p) in self.providers.items()},self.cloud_auto.get(),recheck)
        manager.set_automatic(self.automatic.get())
        with manager.lock:
            manager.settings.update(auto_discover=self.discover.get(),check_software_updates=self.updates.get())
            manager._save()
        if self.autostart_available and self.autostart.get()!=self.initial_autostart:
            __import__('desktop').set_autostart(self.autostart.get())
            self.initial_autostart=self.autostart.get()
        self.app.notice("设置已保存","新的下载、同步和软件更新设置已生效。")

    def clear_choices(self):
        if self.app.storage_moving:
            self.app.notice("请稍候", "资料迁移完成后再清除选择记录。")
            return
        with self.app.manager.lock:
            self.app.manager.settings["large_file_choices"]={}
            self.app.manager.settings["google_warning_choices"]={}
            self.app.manager._save()
        self.app.notice("已清除选择记录","下次遇到需要确认的文件会重新询问。")

    def save_relay(self):
        if self.app.storage_moving:
            raise ValueError("资料迁移完成后再更改聊天设置")
        from urllib.parse import urlsplit
        value=self.relay.get().strip().rstrip("/")
        parsed=urlsplit(value)
        if parsed.scheme!="https" or not parsed.hostname or parsed.path not in {"","/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("请填写专属聊天连接的 HTTPS 地址")
        with self.app.manager.lock:
            self.app.manager.settings["relay_url"]=value
            self.app.manager._save()
        __import__('desktop').chrome_open(value)
