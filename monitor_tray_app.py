"""Windows tray and control panel for the existing DingTalk source monitor."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageTk
import pystray

from monitor_app_control import Controller, read_json, write_json

TITLE = '钉钉货源监控'
BG = '#F4F7FB'
INK = '#18283F'
MUTED = '#718097'
BLUE = '#2563EB'
GREEN = '#0C9463'
AMBER = '#BA6415'
STATE_TEXT = {'running': '正在监听', 'connecting': '正在连接', 'waiting': '等待打开钉钉',
              'paused': '监控已停止', 'stopping': '正在停止', 'offline': '后台未连接'}
STATE_COLOR = {'running': GREEN, 'connecting': BLUE, 'waiting': AMBER,
               'paused': MUTED, 'stopping': AMBER, 'offline': AMBER}


def icon_image(color=BLUE, size=64):
    image = Image.new('RGBA', (128, 128))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 124, 124), radius=30, fill='#101C30')
    draw.rounded_rectangle((22, 25, 105, 91), radius=17, outline='white', width=7)
    draw.polygon([(35, 87), (35, 106), (58, 89)], fill='white')
    draw.line([(35, 59), (48, 59), (57, 43), (69, 75), (79, 59), (92, 59)], fill=color, width=8)
    draw.ellipse((90, 90, 123, 123), fill=color, outline='#101C30', width=4)
    return image.resize((size, size), Image.Resampling.LANCZOS)


class MonitorApp:
    def __init__(self, controller, *, tray_only=False):
        self.controller = controller
        self.commands = queue.SimpleQueue()
        self.busy = False
        self.exiting = False
        self.last_tray_state = None
        self.request_seen = 0
        self.last_recovery = 0
        self.wizard = None
        self.inventory_bridge = None
        self.inventory_service = None
        self.inventory_error = None
        try:
            from inventory_monitor import InventoryService
            self.inventory_service = InventoryService(controller.root)
            from inventory_bridge import start_bridge
            self.inventory_bridge = start_bridge(self.inventory_service)
        except (ImportError, OSError, ValueError):
            self.inventory_error = '库存服务暂不可用，请检查安装文件与本地数据。'
        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.configure(bg=BG)
        self.scale = max(1, self.root.winfo_fpixels('1i') / 96)
        self.s = lambda value: round(value * self.scale)
        source_count = len(read_json(self.controller.root / 'monitor_sources.json', []))
        extra_height = max(0, source_count - 3) * 64
        self.size_window(extra_height)
        self.root.resizable(True, True)
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        self.root.protocol('WM_DELETE_WINDOW', self.hide)
        self.root.bind('<Escape>', lambda event: self.hide())
        self.logo = ImageTk.PhotoImage(icon_image(size=self.s(48)))
        self.root.iconphoto(True, self.logo)
        self.build_window()
        self.tray = pystray.Icon('dingtalk_source_monitor', icon_image(), TITLE,
            menu=pystray.Menu(
                pystray.MenuItem('打开监控面板', lambda *_: self.commands.put('show'), default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('启动监控', lambda *_: self.commands.put('start')),
                pystray.MenuItem('停止监控', lambda *_: self.commands.put('stop')),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('退出程序（停止监控）', lambda *_: self.commands.put('exit'))))
        self.tray.run_detached()
        if tray_only:
            self.root.withdraw()
        self.root.after(200, self.refresh)
        self.root.after(100, self.process_commands)

    def label(self, parent, text='', *, size=10, bold=False, fg=INK, bg=None, **options):
        return tk.Label(parent, text=text, font=('Microsoft YaHei UI', size, 'bold' if bold else 'normal'),
                        fg=fg, bg=bg or parent.cget('bg'), **options)

    def size_window(self, extra_height=0):
        from inventory_panel import window_dimensions
        width, height, minimum_width, minimum_height = window_dimensions(
            self.root.winfo_screenwidth(), self.root.winfo_screenheight(), self.scale, extra_height)
        self.root.minsize(minimum_width, minimum_height)
        self.root.geometry(f'{width}x{height}+30+30')

    def build_window(self):
        header = tk.Frame(self.root, bg='#101C30', padx=self.s(28), pady=self.s(22))
        header.pack(fill='x')
        tk.Label(header, image=self.logo, bg='#101C30').pack(side='left', padx=(0, self.s(16)))
        heading = tk.Frame(header, bg='#101C30')
        heading.pack(side='left')
        self.label(heading, TITLE, size=18, bold=True, fg='white', anchor='w').pack(anchor='w')
        self.label(heading, '价格 · 库存 · 到货', fg='#ADBBD0', size=10).pack(anchor='w', pady=(self.s(4), 0))
        self.button(header, '一键配置', self.configure).pack(side='right')

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill='both', expand=True)
        source_page = tk.Frame(self.tabs, bg=BG)
        self.tabs.add(source_page, text='  消息监控  ')
        inventory_page = tk.Frame(self.tabs, bg=BG)
        self.tabs.add(inventory_page, text='  库存监控  ')
        self.inventory_panel = None
        if self.inventory_service is not None:
            from inventory_panel import InventoryPanel
            self.inventory_panel = InventoryPanel(inventory_page, self.inventory_service, on_hide=self.hide)
            self.inventory_panel.pack(fill='both', expand=True)
            if self.inventory_error:
                self.inventory_panel.notice.set(self.inventory_error)
        else:
            self.label(inventory_page, self.inventory_error, fg=AMBER).pack(pady=30)
        content = tk.Frame(source_page, bg=BG, padx=self.s(28), pady=self.s(19))
        content.pack(fill='both', expand=True)
        status_row = tk.Frame(content, bg=BG)
        status_row.pack(fill='x')
        self.status_dot = self.label(status_row, '●', size=19, fg=BLUE)
        self.status_dot.pack(side='left', padx=(0, self.s(9)))
        self.status_title = self.label(status_row, '正在读取状态', size=19, bold=True)
        self.status_title.pack(side='left')
        self.count_label = self.label(status_row, '正在检查连接', fg=MUTED, size=10)
        self.count_label.pack(side='right')
        self.hint = self.label(content, '关闭窗口后仍在右下角托盘运行。', fg=MUTED, anchor='w')
        self.hint.pack(fill='x', pady=(self.s(4), self.s(16)))

        rows = tk.Frame(content, bg='white', highlightthickness=1, highlightbackground='#E1E7EF')
        rows.pack(fill='both', expand=True)
        self.source_widgets = {}
        sources = read_json(self.controller.root / 'monitor_sources.json', [])
        for index, source in enumerate(sources):
            if index:
                tk.Frame(rows, bg='#EAF0F5', height=1).pack(fill='x', padx=self.s(18))
            row = tk.Frame(rows, bg='white', padx=self.s(18), pady=self.s(12))
            row.pack(fill='both', expand=True)
            self.label(row, source.get('SenderName', '来源'), size=11,
                       bold=True, width=6, anchor='w').pack(side='left')
            self.label(row, source['GroupName'], size=10, fg=MUTED).pack(side='left', padx=self.s(7))
            badge = self.label(row, '待检查', size=10, fg=MUTED, width=10, anchor='e')
            badge.pack(side='right')
            self.source_widgets[source.get('State', source['GroupName'])] = badge

        self.checked = self.label(content, '最近检查：—', size=9, fg=MUTED, anchor='w')
        self.checked.pack(fill='x', pady=(self.s(10), self.s(14)))
        actions = tk.Frame(content, bg=BG)
        actions.pack(fill='x')
        self.start_button = self.button(actions, '启动监控', lambda: self.action('start'), primary=True)
        self.start_button.pack(side='left')
        self.stop_button = self.button(actions, '停止监控', lambda: self.action('stop'))
        self.stop_button.pack(side='left', padx=self.s(10))
        self.button(actions, '收起到托盘', self.hide).pack(side='right')

    def button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, font=('Microsoft YaHei UI', 10, 'bold'),
                         bg=BLUE if primary else '#E7EDF5', fg='white' if primary else INK,
                         activebackground='#1D4ED8' if primary else '#DCE5F0',
                         activeforeground='white' if primary else INK, disabledforeground='#9FAFC6',
                         relief='flat', bd=0, padx=self.s(20), pady=self.s(10), cursor='hand2')

    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.state('normal')
        self.root.lift()
        self.root.focus_force()

    def action(self, name):
        if self.busy:
            return
        if self.wizard is not None and not self.wizard.closed:
            self.wizard.win.lift()
            self.wizard.status.set('请先完成或关闭配置向导，再操作启动、停止或退出。')
            return
        if name == 'start' and not read_json(self.controller.root / 'monitor_sources.json', []):
            self.configure()
            return
        if name == 'exit':
            self.exiting = True
            name = 'stop'
        self.busy = True
        self.hint.config(text='正在启动后台监听…' if name == 'start' else '正在停止监听，请稍候…')

        def work():
            try:
                getattr(self.controller, name)()
                self.commands.put(('done', name))
            except Exception:
                self.commands.put(('error', '操作未完成，请检查 Windows 计划任务“Codex-DingTalk-货源监控”。'))

        threading.Thread(target=work, daemon=True).start()

    def process_commands(self):
        while not self.commands.empty():
            command = self.commands.get()
            if isinstance(command, tuple):
                self.busy = False
                if command[0] == 'error':
                    self.exiting = False
                    self.show()
                    messagebox.showerror(TITLE, command[1], parent=self.root)
            elif command == 'show':
                self.show()
            else:
                self.action(command)
        request = read_json(self.controller.runtime / 'ui_request.json', {})
        if request.get('show', 0) > self.request_seen:
            self.request_seen = request['show']
            self.show()
        self.root.after(150, self.process_commands)

    def refresh(self):
        result = self.controller.status()
        state = result['state']
        color = STATE_COLOR[state]
        self.status_title.config(text=STATE_TEXT[state])
        self.status_dot.config(fg=color)
        self.count_label.config(text=f"{result['ready']} / {len(self.source_widgets)} 已连接")
        if self.inventory_panel is not None:
            self.inventory_panel.message_state.config(text=f"钉钉：{STATE_TEXT[state]} · {result['ready']}/{len(self.source_widgets)} 来源")
        configured = read_json(self.controller.root / 'monitor_sources.json', [])
        group_to_key = {source['GroupName']: source.get('State', source['GroupName']) for source in configured}
        by_group = {source.get('key', group_to_key.get(source['group'], source['group'])): source for source in result['sources']}
        for group, badge in self.source_widgets.items():
            source = by_group.get(group, {})
            if source.get('ready'):
                text, shade = '● 已连接', GREEN
            elif state in ('paused', 'offline'):
                text, shade = '● 已停止', MUTED
            elif state == 'waiting':
                text, shade = '● 等待钉钉', AMBER
            elif state == 'stopping':
                text, shade = '● 正在停止', AMBER
            elif source.get('status', '').startswith(('error', 'connection_error')):
                text, shade = '● 连接异常', AMBER
            else:
                text, shade = '● 正在连接', BLUE
            badge.config(text=text, fg=shade)
        self.checked.config(text='最近检查：' + result['checked_at'])
        if not self.busy:
            messages = {'running': '关闭窗口后仍在右下角托盘运行。',
                        'connecting': '正在连接消息来源，连接失败会自动重试。',
                        'waiting': '打开钉钉后自动监听，也可以点击“启动监控”。',
                        'paused': '点击“启动监控”继续；停止状态会保留。',
                        'stopping': '正在结束监听连接，请稍候。',
                        'offline': '后台尚未连接，点击“启动监控”恢复。'}
            self.hint.config(text=messages[state])
        configuring = self.wizard is not None and not self.wizard.closed
        self.start_button.config(state='disabled' if self.busy or configuring or state in ('running', 'stopping') else 'normal')
        self.stop_button.config(state='disabled' if self.busy or configuring or state in ('paused', 'stopping') else 'normal')
        if state != self.last_tray_state:
            self.tray.icon = icon_image(color)
            self.last_tray_state = state
        self.tray.title = f"{TITLE} · {STATE_TEXT[state]} · {result['ready']}/{len(self.source_widgets)}"
        if (self.controller.settings.get('portable') and configured and state == 'offline'
                and not self.busy and not configuring and not self.exiting and time.monotonic() - self.last_recovery > 30
                and read_json(self.controller.runtime / 'control.json', {}).get('enabled', True)):
            self.last_recovery = time.monotonic()
            threading.Thread(target=self.recover_worker, daemon=True).start()
        if self.exiting and state == 'paused' and not self.busy:
            if self.inventory_bridge is not None:
                self.inventory_bridge.close()
            self.tray.stop()
            self.root.destroy()
            return
        self.root.after(1000, self.refresh)

    def run(self):
        self.root.mainloop()

    def recover_worker(self):
        try:
            self.controller.ensure_task()
        except Exception:
            pass

    def configure(self):
        from monitor_setup_wizard import SetupWizard
        if self.busy or self.exiting:
            return
        self.show()
        if self.wizard is not None and not self.wizard.closed:
            self.wizard.win.lift()
            return
        self.wizard = SetupWizard(self.root, self.controller, on_complete=self.rebuild_panel)

    def rebuild_panel(self):
        for child in self.root.winfo_children():
            if isinstance(child, (tk.Frame, ttk.Notebook)):
                child.destroy()
        count = len(read_json(self.controller.root / 'monitor_sources.json', []))
        self.size_window(max(0, count - 3) * 64)
        self.build_window()


def acquire_instance(runtime, tray_only):
    import msvcrt
    runtime.mkdir(exist_ok=True, parents=True)
    lock = (runtime / 'tray_app.lock').open('a+b')
    if os.fstat(lock.fileno()).st_size == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        if not tray_only:
            write_json(runtime / 'ui_request.json', {'show': time.time()})
        return None
    return lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tray', action='store_true')
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    if args.worker:
        from monitor_portable_install import bundled_dws, default_data_dir
        import source_monitor_supervisor
        try:
            return source_monitor_supervisor.main(root=args.data_dir or default_data_dir(), dws_executable=bundled_dws())
        except Exception as error:
            root = Path(args.data_dir or default_data_dir()) / 'monitor_runtime'
            root.mkdir(parents=True, exist_ok=True)
            with (root / 'supervisor_errors.log').open('a', encoding='utf-8') as log:
                log.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + type(error).__name__ + '\n')
            return 1
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    controller = Controller({'workspace': str(Path(args.data_dir).resolve()), 'portable': True} if args.data_dir else None)
    lock = acquire_instance(controller.runtime, args.tray)
    if lock is None:
        return 0
    app = MonitorApp(controller, tray_only=args.tray)
    app.request_seen = read_json(controller.runtime / 'ui_request.json', {}).get('show', 0)
    if args.configure or not read_json(controller.root / 'monitor_sources.json', []):
        app.root.after(200, app.configure)
    app.run()
    lock.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, '程序无法启动：' + type(error).__name__, TITLE, 0x10)
        raise SystemExit(1)
