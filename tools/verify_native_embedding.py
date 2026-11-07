"""隐藏窗口验收：不访问生产服务，不执行点击、键盘或桌面操作。"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import time
import tkinter as tk


def run():
    root_path=Path(__file__).resolve().parents[1]
    output=root_path/'outputs/订单整合验收/native-embedding'
    output.mkdir(parents=True,exist_ok=True)
    user32=ctypes.windll.user32
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    user32.GetParent.restype=ctypes.c_void_p
    user32.GetParent.argtypes=[ctypes.c_void_p]
    user32.ShowWindow.argtypes=[ctypes.c_void_p,ctypes.c_int]
    user32.IsWindowVisible.argtypes=[ctypes.c_void_p]
    root=tk.Tk()
    root.withdraw()
    try:
        root.overrideredirect(True)
        root.attributes('-alpha',0)
        root.geometry('1080x780+-20000+-20000')
        label=tk.Label(root,text='隔离业务页')
        label.pack(fill='both',expand=True)
        root.deiconify()
        root.update()
        hwnd=user32.GetParent(root.winfo_id())
        user32.ShowWindow(hwnd,0)
        root.attributes('-alpha',1)
        root.update()
        assert not user32.IsWindowVisible(hwnd),'隐藏准备阶段不得显示窗口'
        sizes=[]
        label.bind('<Configure>',lambda event:sizes.append((event.width,event.height)))
        host=root_path/'app/ui/console-host/InventoryConsoleHost.exe'
        # 连续两次模拟关闭和重开宿主，原控件必须仍然存活并可重新布局。
        for attempt in range(2):
            receipt=output/f'check-{attempt}.txt'
            process=subprocess.Popen([str(host),'--embed-check',str(hwnd),str(os.getpid()),str(receipt)],creationflags=subprocess.CREATE_NO_WINDOW)
            deadline=time.monotonic()+15
            while process.poll() is None and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            assert process.poll()==0,'隐藏宿主嵌入验收失败'
            assert receipt.read_text()=='embedded-resized-hidden-detached'
            assert root.winfo_exists() and label.winfo_exists(),'关闭宿主不得销毁业务控件'
            assert not user32.IsWindowVisible(hwnd),'脱离后不得弹出管理窗口'
        assert (600,400) in sizes, f'Tk 控件未收到内容区尺寸：{sizes}'
        (output/'result.json').write_text(json.dumps(dict(passed=True,reopen=2,contentSize=[600,400],desktopShown=False),ensure_ascii=False,indent=2),encoding='utf-8')
        print('隐藏嵌入验收通过：业务控件尺寸同步、关闭脱离、再次嵌入。')
    finally:
        root.destroy()

def verify_business_pages():
    # 真实业务页面只装配隔离样例；禁止网络、ERP 和消息发送，避免假接口掩盖页面缺失。
    import sys
    import tempfile
    import gc
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch
    root_path=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root_path/'app/src'))
    from inventory_refactor_check import seed
    from inventory_workspace import build_workspace
    output=root_path/'outputs/订单整合验收/native-embedding'
    data=Path(tempfile.mkdtemp(prefix='workspace-',dir=output))
    errors=[]
    def blocked(*args,**kwargs):raise AssertionError('隔离业务页禁止外部操作')
    was_enabled=gc.isenabled()
    gc.disable()
    with ExitStack() as stack:
        for target in ('notification_robots.send_encrypted','urllib.request.urlopen','socket.socket.connect',
                       'subprocess.Popen','sql_inventory_source.SqlSource._read_queries','douyin_inventory_runtime.DouyinRuntime.worker'):
            stack.enter_context(patch(target,side_effect=blocked))
        service=seed(data)
        root=tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes('-alpha',0)
        root.geometry('1180x780+-20000+-20000')
        root.report_callback_exception=lambda kind,value,trace:errors.append(str(value))
        controller=SimpleNamespace(root=data,status=lambda:dict(state='paused',ready=0,sources=[]))
        try:
            panel=build_workspace(root,service,live=False,dingtalk_controller=controller,embedded=True)
            root.deiconify();root.update()
            modules=('monitor','dingtalk','daily','profit','events','douyin','settings')
            for module in modules:
                panel.show_module(module);root.update()
                assert panel.active_module==module
                assert panel.winfo_ismapped()
                assert not panel.navigation_buttons['monitor'].winfo_ismapped(),'重复侧栏没有隐藏'
                assert panel.master.winfo_width()==1180,'业务内容未填满右侧容器'
            assert not errors,errors
            (output/'business-pages.json').write_text(json.dumps(dict(passed=True,modules=modules,callbackErrors=errors,productionDataUsed=False),ensure_ascii=False,indent=2),encoding='utf-8')
            print('七个真实业务页隔离装配通过：同一内容容器、无重复侧栏、回调无异常。')
        finally:
            service.close_source()
            if hasattr(panel,'douyin_runtime'):panel.douyin_runtime.close()
            if hasattr(service,'profit_monitor'):service.profit_monitor.stop()
            panel.destroy()
            root.destroy()
    gc.collect()
    if was_enabled:gc.enable()

if __name__=='__main__':
    run()
    verify_business_pages()
