"""Production v1.0 entry: the shared workspace with robot-only live delivery."""
from pathlib import Path
import ctypes
import json
import sys
import time
import tkinter as tk
from tkinter import messagebox
from inventory_monitor import InventoryService
from inventory_bridge import start_bridge
from test_inventory_ui_local import build_workspace
from inventory_version import APP_TITLE


def promote_to_v1(service):
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if service.get(db, 'production_v1_started'):
            return False
        # Keep historical events, but do not deliver the test backlog on release.
        db.execute("UPDATE events SET status='cancelled',retry_at=0 WHERE status IN ('pending','failed')")
        snapshots = db.execute('SELECT sku,data FROM snapshots').fetchall()
        service.put(db, 'pre_v1_snapshots', [[sku, json.loads(data)] for sku, data in snapshots])
        db.execute('DELETE FROM snapshots')
        service.put(db, 'production_v1_started', time.time())
        service.put(db, 'enabled', True)
        service.put(db, 'next_check', 0)
    return True


def stop_dingtalk_and_wait(controller, timeout=15):
    """Do not let the UI report exit while a message listener is still alive."""
    controller.stop()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = controller.status()
        if status['state'] == 'paused' and not any(s.get('live') for s in status['sources']):
            return
        time.sleep(.2)
    raise TimeoutError('钉钉监听尚未确认停止，请稍后重试退出。')


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--tray', action='store_true')
    parser.add_argument('--data-dir')
    parser.add_argument('--ui-check', action='store_true', help='Run an isolated, offline UI check and exit')
    args = parser.parse_args()
    if args.ui_check:
        from inventory_ui_check import run
        return run(args.data_dir)
    from monitor_portable_install import default_data_dir, bundled_dws
    if args.worker:
        import source_monitor_supervisor
        return source_monitor_supervisor.main(root=args.data_dir or default_data_dir(), dws_executable=bundled_dws())
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Local.BackendInventoryMonitor')
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    folder = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    # Retain the existing user data path, including encrypted robot settings.
    service = InventoryService(folder / '.local_inventory_ui_test')
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry(f'{min(1380, root.winfo_screenwidth()-60)}x{min(900, root.winfo_screenheight()-90)}')
    root.minsize(1080, 680)
    root.option_add('*Font', ('Microsoft YaHei UI', 10))
    import queue
    import pystray
    from PIL import Image
    commands = queue.SimpleQueue()
    asset_root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    tray = pystray.Icon('backend_inventory_monitor', Image.open(asset_root / 'monitor_assets' / 'monitor.ico'),
                        '后台库存监控', menu=pystray.Menu(
                            pystray.MenuItem('打开后台库存监控', lambda *_: commands.put('show'), default=True),
                            pystray.MenuItem('退出程序（同时停止钉钉监控）', lambda *_: commands.put('exit'))))
    root.hide_to_tray = root.withdraw
    root.protocol('WM_DELETE_WINDOW', root.withdraw)
    exiting = False
    def process_commands():
        nonlocal exiting
        while not commands.empty():
            command = commands.get()
            if command == 'exit' and not exiting:
                exiting = True
                root.title('正在停止钉钉监控…')
                tray.title = '正在停止钉钉监控…'
                root.withdraw()
                import threading
                def stop_worker():
                    try:
                        stop_dingtalk_and_wait(message_controller)
                        commands.put('exit-ready')
                    except Exception:
                        commands.put('exit-failed')
                threading.Thread(target=stop_worker, daemon=True, name='inventory-shutdown').start()
            elif command == 'exit-ready':
                root.destroy()
                return
            elif command == 'exit-failed':
                exiting = False
                root.title(APP_TITLE)
                tray.title = '后台库存监控'
                root.deiconify()
                messagebox.showerror('暂未退出', '钉钉监控尚未确认停止，请稍后重试退出，或在“钉钉监控”页检查状态。', parent=root)
            elif command == 'show' and not exiting:
                root.deiconify()
                root.lift()
        root.after(150, process_commands)
    # Acquire the collection port before changing any release state.
    try:
        bridge = start_bridge(service, live_delivery=False)
    except OSError:
        messagebox.showerror('已有库存程序运行', '请关闭原库存程序后再打开正式版。', parent=root)
        root.destroy()
        return
    message_controller = None
    try:
        promote_to_v1(service)
        from monitor_app_control import Controller
        message_controller = Controller({'workspace': str(args.data_dir or default_data_dir()), 'portable': True,
                                         'executable': sys.executable if getattr(sys, 'frozen', False) else None})
        with service.db() as db:
            service.put(db, 'daily_source_root', str(message_controller.root))
        try:
            message_controller.ensure_current_worker()
        except RuntimeError as error:
            messagebox.showwarning('钉钉监控暂未启动', str(error), parent=root)
        build_workspace(root, service, live=True, dingtalk_controller=message_controller)
        tray.run_detached()
        root.after(150, process_commands)
        if args.tray:
            root.withdraw()
        from inventory_delivery import start_delivery
        bridge.delivery = start_delivery(service, live=True)
        root.mainloop()
    finally:
        try:
            if message_controller is not None:
                message_controller.stop()
        finally:
            tray.stop()
            bridge.close()


if __name__ == '__main__':
    main()
