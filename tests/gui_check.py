"""Real native clicks through course -> topic -> assignment, settings and confirmation."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'server'))
from desktop import DesktopApp,enable_dpi_awareness
from browser_rpc import save_download
from store import Store

parser=argparse.ArgumentParser();parser.add_argument('--screenshot',type=Path);parser.add_argument('--scale',type=float);args=parser.parse_args()
with tempfile.TemporaryDirectory() as temporary:
    enable_dpi_awareness();root=tk.Tk()
    if args.scale:
        with patch('desktop.dpi_state',return_value={'dpi':round(96*args.scale),'awareness':None}):app=DesktopApp(root,Store(temporary),start_service=False)
    else:app=DesktopApp(root,Store(temporary),start_service=False)
    course=app.manager.add('デジタル演習 · 数字设计课程','https://classroom.google.com/c/course1')
    other=app.manager.add('设计方法 · 参考资料','https://drive.google.com/drive/folders/example2')
    child=app.manager.add('课题一 · 生态调查','https://classroom.google.com/c/course1/a/work1/details',parent_id=course['id'])
    app.manager.project(child['id'])['topic']='第一单元 · 调查与构思'
    saved=save_download(app.store,{'filename':'研究课件.txt','mime':'text/plain','source_url':'https://drive.google.com/file/d/examplefile/view','data_base64':base64.b64encode('完整课件正文'.encode()).decode()})
    app.manager.record_download(child['id'],saved)
    app.manager.project(child['id']).update(status='ready',last_synced=time.time())
    app.manager.project(course['id']).update(status='ready',last_synced=time.time())
    app.broker.connect('synthetic_gui_session_abcdefghijkl')
    def pump(condition=lambda:True,seconds=4):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            app.process_messages();app.refresh();root.update();time.sleep(.025)
            if condition():return
        assert condition(),'GUI condition timed out'
    def click(button):
        root.update();button.event_generate('<ButtonPress-1>',x=8,y=8);button.event_generate('<ButtonRelease-1>',x=8,y=8);root.update()
    pump()
    assert app.classroom.route==('home',)
    click(app.classroom.cards[course['id']])
    assert app.classroom.route==('course',course['id'])
    assert child['id'] not in app.classroom.cards  # Child belongs to the next page, not an expanded tree.
    click(app.classroom.cards['topic:第一单元 · 调查与构思'])
    assert app.classroom.route[0]=='topic'
    click(app.classroom.cards[child['id']])
    assert app.classroom.route==('assignment',child['id'])
    pump(lambda:any(key.startswith('file:') for key in app.classroom.cards))
    file_button=next(button for key,button in app.classroom.cards.items() if key.startswith('file:'))
    with patch.object(app,'open_file') as opened:click(file_button);opened.assert_called_once()
    assert file_button.cget('style')=='File.TButton'
    click(app.classroom.back_button);assert app.classroom.route[0]=='topic'
    click(app.classroom.back_button);assert app.classroom.route[0]=='course'
    app.check_all(True);assert app.checked=={course['id'],other['id'],child['id']}
    app.check_all(False);assert not app.checked
    with patch.object(app.classroom,'render',side_effect=OSError('synthetic transient')):app.refresh()
    delivered=[];app.messages.put(lambda:delivered.append(True));app.process_messages();assert delivered==[True]
    with ThreadPoolExecutor() as executor:
        future=executor.submit(app.confirm_large_download,{'filename':'large.pdf','bytes':1024,'threshold_bytes':512,'timeout_seconds':1})
        pump(future.done)
        assert future.result()=={'allowed':False,'remember':False}
        future=executor.submit(app.confirm_large_download,{'filename':'large.pdf','bytes':1024,'threshold_bytes':512,'timeout_seconds':5})
        pump(lambda:any(isinstance(w,tk.Toplevel) for w in root.winfo_children()))
        dialog=next(w for w in root.winfo_children() if isinstance(w,tk.Toplevel))
        root.tk.call(dialog.protocol('WM_DELETE_WINDOW'));pump(future.done)
        assert future.result()=={'allowed':False,'remember':False}
    app.notice('合成结果通知','不应阻塞后续消息');app.messages.put(lambda:delivered.append(True));app.process_messages();assert delivered==[True,True]
    for dialog in [w for w in root.winfo_children() if isinstance(w,tk.Toplevel)]:dialog.destroy()
    app.preferences();root.update();settings=app.settings_window
    assert len(settings.tabs.tabs())==5
    for index in range(5):settings.tabs.select(index);root.update();assert settings.frames[index].winfo_ismapped()
    assert settings.location.cget('text')==str(app.store.root)
    def footer_buttons(widget):
        result=[]
        for w in widget.winfo_children():
            if isinstance(w,ttk.Button) and w.cget('text') in {'保存设置','关闭'}:result.append(w)
            result+=footer_buttons(w)
        return result
    for button in footer_buttons(settings):
        assert button.winfo_ismapped()
        assert button.winfo_rooty()+button.winfo_height()<=settings.winfo_rooty()+settings.winfo_height()
        assert button.winfo_rootx()+button.winfo_width()<=settings.winfo_rootx()+settings.winfo_width()
    settings.destroy()
    # Waiting may be cancelled before copying; no database location is switched.
    before_root=app.store.root
    with app.job_lock:app.background_jobs+=1
    app.change_storage(Path(temporary).resolve().with_name('unused-cancel-target'))
    app.cancel_storage_wait()
    pump(lambda:not app.storage_moving,seconds=5)
    with app.job_lock:app.background_jobs-=1
    assert app.store.root==before_root
    # A real UI-triggered storage switch preserves live handlers, extension and navigation.
    app.manager._save()
    moved=tempfile.TemporaryDirectory()
    destination=(Path(moved.name)/'materials').resolve()
    extension_folder=app.extension_folder
    app.change_storage(destination,remove_old=True)
    pump(lambda:not app.storage_moving,seconds=10)
    assert app.store.root==destination
    assert not app.manager.maintenance
    assert app.extension_folder==extension_folder and extension_folder.is_dir()
    app.classroom.navigate(('assignment',child['id']))
    pump(lambda:any(key.startswith('file:') for key in app.classroom.cards))
    for dialog in [w for w in root.winfo_children() if isinstance(w,tk.Toplevel)]:dialog.destroy()
    with patch('updates.check',return_value={'version':'0.7.1','size':32,'notes':'fixture'}) as checked,patch.object(app,'offer_update') as offered:
        app.check_software_update(manual=True)
        pump(lambda:not app.update_busy)
        checked.assert_called_once()
        offered.assert_called_once()
    # Right-hand content is a native page; no HTML file or browser is launched.
    with patch('desktop.chrome_open') as launch,patch('desktop.messagebox.showinfo') as info:
        app.connect_browser();launch.assert_not_called();info.assert_not_called()
    if args.screenshot:
        from PIL import ImageGrab
        app.classroom.navigate(('topic',course['id'],'第一单元 · 调查与构思'));root.lift();root.update();time.sleep(.3)
        x,y=root.winfo_rootx(),root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+root.winfo_width(),y+root.winfo_height())).save(args.screenshot)
    app.quit()
    moved.cleanup()
print('Native Classroom page drill-down, blue file links, five settings pages, message recovery and confirmation timeout/close passed (synthetic).')
