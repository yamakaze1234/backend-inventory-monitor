"""Production v1.0 entry: the shared workspace with robot-only live delivery."""
from project_paths import PROJECT_ROOT, UI_ROOT
from pathlib import Path
import ctypes
import json
import sys
import time
import threading
import tkinter as tk
from tkinter import messagebox
from inventory_sources import InventoryRuntimeService as InventoryService
from inventory_bridge import start_bridge
from inventory_workspace import build_workspace
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
        # 新用户没有产品时先显示配置引导，不自动启动采集或发送。
        service.put(db, 'enabled', service.get(db, 'enabled', bool(db.execute('SELECT 1 FROM products LIMIT 1').fetchone())))
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


def start_configured_message_worker(controller):
    """Inventory works independently until message sources are configured."""
    from monitor_app_control import read_json
    if not read_json(controller.root / 'monitor_sources.json', []):
        return False
    controller.ensure_current_worker()
    return True


def stop_order_bot_and_wait(bot, timeout=90):
    """等待原匹配后端结束当前批次，不通过结束进程打断 ERP 写入。"""
    bot.action('stop')
    deadline=time.monotonic()+timeout
    while not bot.action('finish-stop').get('stopped'):
        if time.monotonic()>=deadline:
            raise TimeoutError('订单工作进程尚未安全停止')
        time.sleep(1)


def clear_update_restore_flags(service):
    """补偿或启动恢复只消费一次，不能在用户后续暂停后重新启用旧意图。"""
    with service.db() as db:
        values={key:service.get(db,key,False) for key in ('update_restore_order_bot','update_restore_order_owned','update_restore_messages')}
        for key in values:
            service.put(db,key,False)
    return values


def restore_update_services(service, controller, console):
    """恢复失败仍保留工作台，并显示需手动处理的服务，不循环重启。"""
    values=clear_update_restore_flags(service)
    failures=[]
    if values['update_restore_messages']:
        try:
            controller.start()
        except Exception:
            failures.append('钉钉监听未恢复，请在钉钉监控页检查并启动')
    if values['update_restore_order_bot']:
        try:
            console.bot.action('start')
            console.owns_bot=values['update_restore_order_owned']
        except Exception:
            failures.append('订单机器人未恢复，请在订单匹配页检查并启动')
    if failures:
        console.updater.state.update(phase='error',error='；'.join(failures))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--tray', action='store_true')
    parser.add_argument('--data-dir')
    # 保留原生界面回退入口；更新回执仅在本机服务与窗口装配成功后写入。
    parser.add_argument('--legacy-ui', action='store_true', help='使用原生管理界面')
    parser.add_argument('--console-preview-data', type=Path, help='独立数据目录中的 ReUI 离线验收')
    parser.add_argument('--console-headless', action='store_true', help='不打开窗口的候选包验收')
    parser.add_argument('--console-preview-seconds', type=int, default=0, help='隔离验收自动退出秒数')
    parser.add_argument('--update-health', type=Path, help='更新辅助程序的启动回执路径')
    parser.add_argument('--ui-check', action='store_true', help='Run an isolated, offline UI check and exit')
    parser.add_argument('--sql-ui-check', action='store_true', help='Run isolated SQL/script acceptance and exit')
    parser.add_argument('--refactor-check', action='store_true', help='离线验证重构界面，不启动采集或通知')
    parser.add_argument('--browser-check', action='store_true', help='在独立空目录验证本机浏览器连接，不访问真实店铺')
    parser.add_argument('--offline-demo', action='store_true', help='使用独立模拟数据体验重构界面')
    parser.add_argument('--douyin-erp-check', help='只读复核整机快照的 ERP 配件，保存相邻结果文件，不发送消息')
    args = parser.parse_args()
    # 正式 EXE 自行加载便携环境，不要求通过 CMD 或启动器进入。
    if getattr(sys, 'frozen', False):
        from inventory_portable import configure_environment
        configure_environment(Path(sys.executable).parent)
    if args.console_preview_data:
        from inventory_console import preview
        return preview(args.console_preview_data, desktop=not args.console_headless,
                       duration=max(0,args.console_preview_seconds) or None, check_host=args.console_headless)
    if args.browser_check:
        from inventory_browser_check import run
        if not args.data_dir:
            parser.error('--browser-check 需要独立的 --data-dir 验收输出目录')
        return run(args.data_dir)
    if args.douyin_erp_check:
        # 使用与桌面相同的真实打包环境验收 ERP，不启动巡检或消息线程。
        from douyin_erp import attach_erp
        from douyin_scope import MACHINE_SCOPE
        target = Path(args.douyin_erp_check)
        payload = json.loads(target.read_text(encoding='utf-8'))
        products = payload['products']
        if not products or any(p.get('monitor_scope') != MACHINE_SCOPE for p in products):
            raise ValueError('复核文件必须为已核验整机类目的完整快照')
        service = InventoryService(Path(args.data_dir))
        with service.db() as db:
            settings = service.get(db, 'sql_connection', {})
        service.configure_sql(settings, '', remember=True)
        attach_erp(service, payload['shop'], products)
        target.with_suffix('.result.json').write_text(json.dumps(dict(shop=payload['shop'], products=products), ensure_ascii=False), encoding='utf-8')
        return
    if args.refactor_check or args.offline_demo:
        from inventory_refactor_check import run
        return run(args.data_dir, interactive=args.offline_demo)
    if args.sql_ui_check:
        from inventory_sql_ui_check import run
        return run(args.data_dir)
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
    folder = Path(sys.executable).parent if getattr(sys, 'frozen', False) else PROJECT_ROOT
    from inventory_workspace_config import resolve_data_root, message_data_root
    data_root = resolve_data_root(args.data_dir, folder)
    from inventory_portable import initialize_workspace
    initialize_workspace(folder, data_root)
    # 旧安装保留原数据目录；新目录只使用本工作区自己的机器人与监听设置。
    legacy_workspace = data_root == (folder / '.local_inventory_ui_test').resolve()
    service = InventoryService(data_root)
    root = tk.Tk()
    # 初始化期间不暴露未完成绘制的原生窗口，避免 Windows 显示黑底半成品。
    root.withdraw()
    root.configure(bg='#F5F6F8')
    root.title(APP_TITLE)
    root.geometry(f'{min(1380, root.winfo_screenwidth()-60)}x{min(900, root.winfo_screenheight()-90)}')
    root.minsize(1080, 680)
    root.option_add('*Font', ('Microsoft YaHei UI', 10))
    import queue
    import pystray
    from PIL import Image
    commands = queue.SimpleQueue()
    asset_root = UI_ROOT
    tray = pystray.Icon('backend_inventory_monitor', Image.open(asset_root / 'monitor_assets' / 'monitor.ico'),
                        '后台库存监控', menu=pystray.Menu(
                            pystray.MenuItem('打开后台库存监控', lambda *_: commands.put('show'), default=True),
                            pystray.MenuItem('退出程序（同时停止钉钉监控）', lambda *_: commands.put('exit'))))
    root.hide_to_tray = root.withdraw
    root.protocol('WM_DELETE_WINDOW', root.withdraw)
    exiting = False
    console = None
    installing = False
    def process_commands():
        nonlocal exiting, installing
        while not commands.empty():
            command = commands.get()
            if command == 'exit' and not exiting:
                exiting = True
                if console is not None:
                    console.closing = True
                root.title('正在停止钉钉监控…')
                tray.title = '正在停止钉钉监控…'
                root.withdraw()
                def stop_worker():
                    try:
                        if console is not None:
                            # 等正在执行的启动操作返回后再判断接管状态，消除退出与启动竞态。
                            with console.action_lock:
                                deadline=time.monotonic()+180
                                while console.business and console.business.busy():
                                    if time.monotonic()>deadline:raise TimeoutError('业务操作尚未结束')
                                    time.sleep(.2)
                                if console.owns_bot:
                                    stop_order_bot_and_wait(console.bot)
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
                if console is not None:
                    console.closing = False
                root.title(APP_TITLE)
                tray.title = '后台库存监控'
                if args.legacy_ui:
                    root.deiconify()
                else:
                    console.open()
                messagebox.showerror('暂未退出', '业务任务或钉钉监控尚未确认结束，请查看任务状态后重试退出。', parent=root)
            elif command == 'show' and not exiting:
                if console is not None and not args.legacy_ui:
                    console.open()
                else:
                    root.deiconify()
                    root.lift()
            elif command == 'install-update' and not exiting:
                exiting = installing = True
                console.closing = True
                def prepare_update():
                    restore_bot = False
                    restore_messages = False
                    console.action_lock.acquire()
                    try:
                        if console.business and console.business.busy():raise ValueError('业务操作尚未结束，稍后更新')
                        console.updater.state['phase'] = 'preparing'
                        state = console.bot.snapshot()
                        if state.get('active', 0):
                            raise ValueError('订单任务仍在处理中，请完成或核验后更新')
                        restore_bot = bool(state.get('available') and state.get('serviceEnabled'))
                        if state.get('available') and (restore_bot or state.get('workerOnline')):
                            # 先关闭接单入口，再等原执行进程结束，避免检查与替换之间插入新任务。
                            stop_order_bot_and_wait(console.bot)
                        if console.bot.snapshot().get('active', 0):
                            raise ValueError('仍有排队或待核验订单任务，取消安装')
                        restore_messages = message_controller.status().get('state') in ('running','connecting','waiting')
                        stop_dingtalk_and_wait(message_controller)
                        with service.db() as db:
                            service.put(db, 'update_restore_order_bot', restore_bot)
                            service.put(db, 'update_restore_order_owned', console.owns_bot)
                            service.put(db, 'update_restore_messages', restore_messages)
                        console.updater.install()
                        commands.put('exit-ready')
                    except Exception as error:
                        clear_update_restore_flags(service)
                        recovery_errors=[]
                        if restore_bot:
                            try:
                                console.bot.action('start')
                            except Exception:
                                recovery_errors.append('订单机器人未恢复，请手动检查')
                        if restore_messages:
                            try:
                                message_controller.start()
                            except Exception:
                                recovery_errors.append('钉钉监听未恢复，请手动检查')
                        console.updater.state.update(phase='error', error='；'.join([str(error),*recovery_errors]))
                        commands.put('update-failed')
                    finally:
                        console.action_lock.release()
                threading.Thread(target=prepare_update, daemon=True, name='inventory-update-shutdown').start()
            elif command == 'update-failed':
                exiting = installing = False
                console.closing = False
        root.after(150, process_commands)
    # Acquire the collection port before changing any release state.
    try:
        bridge = start_bridge(service, live_delivery=False)
        from inventory_browser_recovery import BrowserRecovery
        bridge.native = BrowserRecovery(service).start()
    except OSError:
        messagebox.showerror('已有库存程序运行', '请关闭原库存程序后再打开正式版。', parent=root)
        root.destroy()
        return
    message_controller = None
    try:
        promote_to_v1(service)
        from monitor_app_control import Controller
        message_controller = Controller({'workspace': str(message_data_root(service, legacy=legacy_workspace)), 'portable': True,
                                         'executable': sys.executable if getattr(sys, 'frozen', False) else None})
        with service.db() as db:
            service.put(db, 'daily_source_root', str(message_controller.root))
        try:
            start_configured_message_worker(message_controller)
        except RuntimeError as error:
            messagebox.showwarning('钉钉监控暂未启动', str(error), parent=root)
        # 默认主界面完全由 React / ReUI 承载；Tk 只保留托盘事件循环和显式旧版回退。
        panel = build_workspace(root,service,live=True,dingtalk_controller=message_controller) if args.legacy_ui else None
        from douyin_inventory_runtime import DouyinRuntime
        runtime = panel.ensure_douyin_runtime() if panel else DouyinRuntime(service,live=True)
        from inventory_business import BusinessConsole
        from inventory_console import InventoryConsole
        business=BusinessConsole(service,message_controller,runtime)
        console=InventoryConsole(service,native_action=commands.put,business=business)
        service.start_source()
        from profit_monitor import ProfitMonitor, CoordinatedProfitSource
        from notification_robots import store_for_service
        service.profit_monitor = ProfitMonitor(service.root,
            source_provider=lambda: CoordinatedProfitSource(service.sql_worker),
            robots=store_for_service(service))
        service.profit_monitor.start()
        from inventory_cache_schedule import start_worker
        start_worker(service,business=business,runtime=runtime)
        if panel is not None and service.source_mode() == 'sql' and service.sql_worker.source is None:
            panel.show_module('settings')
        tray.run_detached()
        root.after(150, process_commands)
        if not args.tray:
            # 先让布局与图片进入就绪状态，再由事件循环显示完整窗口。
            root.update_idletasks()
            root.after_idle(root.deiconify if args.legacy_ui else console.open)
        # 与网页操作共用串行锁，恢复线程不能越过已经开始的退出流程。
        def restore_services():
            with console.action_lock:
                if not console.closing:
                    restore_update_services(service, message_controller, console)
        threading.Thread(target=restore_services, daemon=True, name='restore-update-services').start()
        from inventory_delivery import start_delivery
        bridge.delivery = start_delivery(service, live=True)
        if args.update_health:
            import re
            from inventory_version import VERSION
            # 随机回执名避免重试更新时误认上一轮留下的健康标记。
            if (args.update_health.resolve().parent == (service.root / 'updates').resolve()
                    and re.fullmatch(r'healthy-[a-f0-9]{32}\.txt', args.update_health.name)):
                root.after_idle(lambda: args.update_health.write_text(VERSION, encoding='utf-8'))
        root.mainloop()
    finally:
        try:
            if message_controller is not None:
                message_controller.stop()
        finally:
            from inventory_cache_schedule import stop_worker
            stop_worker(service)
            tray.stop()
            service.close_source()
            if hasattr(service, 'profit_monitor'):
                service.profit_monitor.stop()
            bridge.close()
            if console is not None:
                console.close()
                if console.business and console.business.douyin:
                    console.business.douyin.close()


if __name__ == '__main__':
    main()
