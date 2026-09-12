"""Embedded controls for the existing DingTalk supervisor and setup wizard."""
import queue
import threading
import tkinter as tk
from tkinter import ttk
from monitor_app_control import read_json
from inventory_theme import ScrollBar, button, wrap_label, MUTED, INK


class DingTalkPanel(tk.Frame):
    def __init__(self, master, controller):
        super().__init__(master, bg='white', padx=2, pady=2)
        self.controller = controller
        self.events = queue.SimpleQueue()
        self.busy = False
        self.wizard = None
        self.after_id = None
        self.status_label = tk.Label(self, text='正在读取钉钉状态', bg='white', fg='#141923', font=('Microsoft YaHei UI', 16, 'bold'))
        self.status_label.pack(anchor='w', pady=(0, 8))
        self.hint = tk.StringVar(self, '沿用现有消息来源和机器人配置。')
        hint_label = tk.Label(self, textvariable=self.hint, bg='white', fg=MUTED, anchor='w', justify='left')
        hint_label.pack(fill='x', pady=(0, 16))
        wrap_label(hint_label)
        actions = tk.Frame(self, bg='white')
        actions.pack(side='bottom', fill='x', pady=(16, 0))
        self.buttons = []
        for title, command in [('启动钉钉监控', lambda: self.action('start')), ('停止钉钉监控', lambda: self.action('stop')), ('一键配置', self.configure_sources), ('管理机器人', self.manage_robots)]:
            control = button(actions, title, command, primary=not self.buttons)
            control.pack(side='left', padx=(0, 10))
            self.buttons.append(control)
        table_area = tk.Frame(self, bg='white')
        table_area.pack(fill='both', expand=True)
        self.table = ttk.Treeview(table_area, columns=('sender', 'group', 'status'), show='headings')
        for key, title, width in [('sender', '监控人员', 110), ('group', '来源群', 300), ('status', '连接状态', 120)]:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=90, stretch=key == 'group')
        scrollbar = ScrollBar(table_area, command=self.table.yview)
        horizontal = ScrollBar(table_area, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table_area.columnconfigure(0, weight=1)
        table_area.rowconfigure(0, weight=1)
        self.refresh()

    def action(self, name):
        if self.busy:
            return
        if name == 'start' and not read_json(self.controller.root / 'monitor_sources.json', []):
            self.configure_sources()
            return
        self.busy = True
        self.hint.set('正在启动监听…' if name == 'start' else '正在停止监听…')
        def work():
            try:
                getattr(self.controller, name)()
                self.events.put('操作已提交，连接状态将自动更新。')
            except Exception:
                self.events.put('操作未完成，请检查钉钉客户端及一键配置。')
        threading.Thread(target=work, daemon=True).start()

    def manage_robots(self):
        from notification_robot_ui import open_robot_manager
        if getattr(self, 'robot_window', None) is not None and self.robot_window.winfo_exists():
            self.robot_window.lift()
            return
        self.robot_window = open_robot_manager(self, self.controller.root)

    def configure_sources(self):
        from monitor_setup_wizard import SetupWizard
        if self.wizard is not None and not self.wizard.closed:
            self.wizard.win.lift()
            return
        self.wizard = SetupWizard(self.winfo_toplevel(), self.controller)

    def refresh(self):
        if not self.winfo_ismapped():
            self.after_id = self.after(500, self.refresh)
            return
        while not self.events.empty():
            self.hint.set(self.events.get_nowait())
            self.busy = False
        try:
            status = self.controller.status()
            sources = read_json(self.controller.root / 'monitor_sources.json', [])
            labels = {'running': '正在监听', 'connecting': '正在连接', 'waiting': '等待打开钉钉', 'paused': '已停止', 'stopping': '正在停止', 'offline': '后台未连接'}
            self.status_label.config(text=f"钉钉监控 · {labels.get(status['state'], status['state'])}  {status['ready']} / {len(sources)} 已连接",
                                     fg='#087B54' if status['state'] == 'running' else '#BA6415')
            runtime = {s['key']: s for s in status['sources']}
            from inventory_theme import sync_tree
            rows = []
            for index, source in enumerate(sources):
                current = runtime.get(source.get('State', source.get('GroupName')), {})
                label = '已连接' if current.get('ready') else ('连接中' if current.get('live') else '未连接')
                rows.append((str(index), (source.get('SenderName', ''), source.get('GroupName', ''), label), ()))
            sync_tree(self.table, rows)
            for button in self.buttons:
                button.config(state='disabled' if self.busy else 'normal')
        except Exception:
            self.hint.set('读取钉钉监控状态失败，原配置保持不变。')
        self.after_id = self.after(2000, self.refresh)

    def destroy(self):
        if self.after_id:
            self.after_cancel(self.after_id)
        super().destroy()
