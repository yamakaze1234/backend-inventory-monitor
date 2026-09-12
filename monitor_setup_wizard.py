"""A plain-language first-run wizard; all network/install work runs off the UI thread."""
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from monitor_setup_service import SetupService, SetupError, DEFAULT_ROWS
from monitor_app_control import write_json, read_json
from inventory_theme import install_theme, ScrollBar, bind_wheel, BG, MUTED, wrap_label, button, section


class SetupWizard:
    def __init__(self, parent, controller, on_complete=None, service=None):
        self.controller = controller
        self.service = service or SetupService(controller.root, portable=controller.settings.get('portable', False))
        self.on_complete = on_complete
        self.events = queue.SimpleQueue()
        self.closed = False
        self.busy = False
        self.rows = self.service.initial_rows()
        self.resolved = None
        self.accounts = []
        self.installed = (not controller.settings.get('portable', False) or
                          bool(read_json(controller.root / 'installation.json', {}).get('executable')))
        self.win = tk.Toplevel(parent)
        self.win.title('一键配置 · 钉钉货源监控')
        self.win.configure(bg=BG)
        install_theme(self.win)
        self.win.geometry('900x760')
        self.win.minsize(780, 560)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self.win.transient(parent)
        self.win.lift()
        self.status = tk.StringVar(value='不用安装 Python，不用输入命令。按下面三步完成配置。')
        self.account = tk.StringVar()
        self.webhook = tk.StringVar()
        self.make_ui()
        self.win.after(100, self.pump)
        self.win.after(300, self.refresh_accounts)

    def make_ui(self):
        header = tk.Frame(self.win, bg='#101720', padx=24, pady=18)
        header.pack(fill='x')
        tk.Label(header, text='配置钉钉监控', fg='white', bg='#101720',
                 font=('Microsoft YaHei UI', 19, 'bold')).pack(anchor='w')
        tk.Label(header, text='准备程序 → 登录钉钉 → 确认监控与通知', fg='#B8C6DA', bg='#101720',
                 font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(6, 0))
        footer = tk.Frame(self.win, bg=BG, padx=24, pady=14)
        footer.pack(side='bottom', fill='x')
        surface = tk.Frame(self.win, bg=BG)
        surface.pack(fill='both', expand=True)
        canvas = tk.Canvas(surface, bg=BG, highlightthickness=0)
        scrollbar = ScrollBar(surface, command=canvas.yview, bg=BG)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = tk.Frame(canvas, bg=BG, padx=24, pady=14)
        window = canvas.create_window((0, 0), window=body, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        bind_wheel(canvas)
        first_card = section(body, '1  准备程序与登录')
        first = tk.Frame(first_card, bg='white')
        first.pack(fill='x')
        self.prepare_button = button(first, text='一键安装到本机', command=self.prepare)
        self.prepare_button.grid(row=0, column=0, padx=(0, 8), pady=(0, 10))
        if self.installed:
            self.prepare_button.config(text='程序已准备', state='disabled')
        button(first, text='登录钉钉', command=self.login).grid(row=0, column=1, padx=(0, 8), pady=(0, 10))
        button(first, text='刷新账号', command=self.refresh_accounts).grid(row=0, column=2, pady=(0, 10))
        ttk.Label(first, text='使用账号').grid(row=1, column=0, sticky='w')
        self.account_box = ttk.Combobox(first, textvariable=self.account, state='readonly', width=65)
        self.account_box.grid(row=1, column=1, columnspan=3, sticky='ew')
        self.account_box.bind('<<ComboboxSelected>>', lambda e: self.invalidate())
        first.columnconfigure(3, weight=1)

        second = section(body, '2  监控谁的消息')
        table_area = ttk.Frame(second)
        table_area.pack(fill='both', expand=True)
        self.table = ttk.Treeview(table_area, columns=('group', 'person', 'terms'), show='headings', height=5)
        for key, label, width in [('group', '群名称', 225), ('person', '发送人', 75), ('terms', '关注关键词', 340)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=60)
        scroll = ScrollBar(table_area, command=self.table.yview)
        horizontal = ScrollBar(table_area, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table_area.columnconfigure(0, weight=1)
        table_area.rowconfigure(0, weight=1)
        self.table.bind('<Double-1>', lambda e: self.edit_row())
        buttons = ttk.Frame(second)
        buttons.pack(fill='x', pady=(8, 0))
        for text, fn in [('添加', self.add_row), ('修改选中', self.edit_row), ('移除选中', self.remove_row),
                         ('恢复四路模板', self.restore_template)]:
            button(buttons, text=text, command=fn).pack(side='left', padx=(0, 6))
        self.validate_button = button(second, text='一键核对群和人员', command=self.validate, primary=True)
        self.validate_button.pack(anchor='e', pady=(12, 0))
        self.render_rows()

        third = section(body, '3  发到哪个接收群')
        from notification_robot_ui import RobotPanel
        self.robot_panel = RobotPanel(third, self.controller.root)
        self.robot_panel.pack(fill='x')

        status_label = tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED,
                               justify='left', anchor='w')
        status_label.pack(fill='x', pady=(0, 10))
        wrap_label(status_label)
        self.save_button = button(footer, text='保存并启动监控', command=self.save, state='disabled', primary=True)
        self.save_button.pack(side='right')
        button(footer, text='稍后配置', command=self.close).pack(side='right', padx=10)
        tk.Label(footer, text='启动后自动转发符合条件的新消息', bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='left')

    def invalidate(self):
        self.resolved = None
        self.save_button.config(state='disabled')

    def render_rows(self):
        self.table.delete(*self.table.get_children())
        for i, row in enumerate(self.rows):
            self.table.insert('', 'end', iid=str(i), values=(row['GroupName'], row['SenderName'], row['Terms']))

    def selected_index(self):
        selection = self.table.selection()
        return int(selection[0]) if selection else None

    def edit_row(self, index=None):
        if self.busy:
            return
        index = self.selected_index() if index is None else index
        if index is None:
            return
        row = self.rows[index]
        popup = tk.Toplevel(self.win)
        popup.title('监控对象')
        popup.configure(bg=BG, padx=18, pady=18)
        content = section(popup, '监控对象')
        fields = tk.Frame(content, bg='white')
        fields.pack(fill='x')
        fields.columnconfigure(1, weight=1)
        popup.transient(self.win)
        popup.grab_set()
        values = {}
        for n, (key, label) in enumerate([('GroupName', '群完整名称'), ('SenderName', '发送人姓名'), ('Terms', '关键词（逗号分隔）')]):
            ttk.Label(fields, text=label).grid(row=n, column=0, padx=12, pady=10, sticky='w')
            values[key] = tk.StringVar(value=row[key])
            ttk.Entry(fields, textvariable=values[key], width=42).grid(row=n, column=1, padx=12, pady=10, sticky='ew')
        allow = tk.BooleanVar(value=row.get('AllowReopen', False))
        ttk.Checkbutton(fields, text='包含成本表重开消息', variable=allow).grid(row=3, column=1, sticky='w', padx=12)
        def apply():
            if not all(value.get().strip() for value in values.values()):
                messagebox.showinfo('请填写完整', '群名、发送人、关键词都需要填写。', parent=popup)
                return
            self.rows[index] = {key: value.get().strip() for key, value in values.items()}
            self.rows[index]['AllowReopen'] = allow.get()
            self.invalidate()
            self.render_rows()
            popup.destroy()
        button(fields, text='确定', command=apply, primary=True).grid(row=4, column=1, sticky='e', padx=12, pady=12)

    def add_row(self):
        if not self.busy:
            self.rows.append(dict(GroupName='', SenderName='', Terms='库存,价格,到货', AllowReopen=False))
            self.invalidate()
            self.render_rows()
            self.edit_row(len(self.rows) - 1)

    def remove_row(self):
        index = self.selected_index()
        if not self.busy and index is not None:
            self.rows.pop(index)
            self.invalidate()
            self.render_rows()

    def restore_template(self):
        if not self.busy:
            self.rows = [dict(r) for r in DEFAULT_ROWS]
            self.invalidate()
            self.render_rows()

    def background(self, kind, operation, message):
        if self.busy:
            return
        self.busy = True
        self.account_box.config(state='disabled')
        self.status.set(message)
        self.save_button.config(state='disabled')
        def run():
            try:
                self.events.put((kind, operation()))
            except Exception as error:
                self.events.put(('error', str(error) if isinstance(error, SetupError) else '操作未完成，请检查网络或安装目录权限后重试。'))
        threading.Thread(target=run, daemon=True).start()

    def prepare(self):
        from monitor_portable_install import install_current_user
        self.background('installed', lambda: install_current_user(self.controller.root), '正在安装程序并创建桌面图标…')

    def refresh_accounts(self):
        self.background('profiles', self.service.profiles, '正在检测本机钉钉授权…')

    def login(self):
        self.background('profiles', self.service.login, '已打开浏览器，请本人完成钉钉登录和授权，然后回到这里。')

    def validate(self):
        index = self.account_box.current()
        if index < 0:
            self.status.set('请先登录钉钉，并在“使用账号”中选择本次账号。')
            return
        profile = self.accounts[index]['key']
        rows = [dict(r) for r in self.rows]
        self.invalidate()
        def operation():
            self.service.check_account(profile)
            return self.service.resolve_sources(rows, profile, choose=self.choose,
                       progress=lambda text: self.events.put(('progress', text)))
        self.background('resolved', operation, '正在核对群和人员，不发送任何消息…')

    def choose(self, title, candidates):
        event = threading.Event()
        result = []
        self.events.put(('choice', (title, candidates, event, result)))
        event.wait()
        return result[0] if result else None

    def choose_dialog(self, payload):
        title, candidates, event, result = payload
        popup = tk.Toplevel(self.win)
        popup.title('请选择实际对象')
        popup.configure(bg=BG, padx=18, pady=18)
        content = section(popup, '请选择实际对象')
        popup.transient(self.win)
        popup.grab_set()
        ttk.Label(content, text=title + ' 有多个结果，请选择：').pack(padx=15, pady=12)
        selected = tk.IntVar(value=-1)
        for i, candidate in enumerate(candidates):
            ttk.Radiobutton(content, text=candidate['label'], variable=selected, value=i).pack(anchor='w', padx=15, pady=5)
        def finish():
            if selected.get() >= 0:
                result.append(candidates[selected.get()])
            event.set()
            popup.destroy()
        button(content, text='确定', command=finish, primary=True).pack(pady=12)
        popup.protocol('WM_DELETE_WINDOW', finish)

    def save(self):
        if not self.resolved:
            return
        if not self.installed:
            self.status.set('请先点击“一键安装到本机”，再保存启动。')
            return
        sources = list(self.resolved)
        webhook = self.webhook.get()
        self.background('saved', lambda: self.service.save(sources, webhook, self.controller),
                        '正在保存配置并启动监听…')

    def delivery_help(self):
        messagebox.showinfo('获取接收地址',
            '在钉钉打开接收通知的群：\n\n'
            '1. 打开群设置中的机器人/智能群助手。\n'
            '2. 添加“自定义机器人”（需有相应群权限）。\n'
            '3. 安全设置选择关键词，填写：价格调整。\n'
            '4. 复制机器人 Webhook 地址，粘贴到本窗口。\n\n'
            '本版按关键词机器人配置，不支持加签模式。\n'
            '这里只检查地址格式，不会发送测试消息。', parent=self.win)

    def pump(self):
        if self.closed:
            return
        while not self.events.empty():
            kind, value = self.events.get()
            if kind == 'progress':
                self.status.set(value)
                continue
            if kind == 'choice':
                self.choose_dialog(value)
                continue
            self.busy = False
            self.account_box.config(state='readonly')
            if kind == 'error':
                self.status.set(value)
            elif kind == 'installed':
                self.installed = True
                self.controller.settings['executable'] = value['executable']
                write_json(self.controller.root / 'installation.json', value)
                self.prepare_button.config(text='安装完成', state='disabled')
                self.status.set('安装完成。桌面图标和开机启动已配置，请继续登录钉钉。')
            elif kind == 'profiles':
                self.accounts = value
                self.account_box.config(values=[p['label'] for p in value])
                self.account.set('')
                current = [i for i, p in enumerate(value) if p['current']]
                if len(current) == 1:
                    self.account_box.current(current[0])
                self.invalidate()
                self.status.set('请选择账号，再核对监控对象。' if value else '尚未登录，请点击“登录钉钉”。')
            elif kind == 'resolved':
                self.resolved = value
                self.save_button.config(state='normal')
                self.status.set(f'已核对 {len(value)} 路监控。确认接收地址后，点击“保存并启动监控”。')
            elif kind == 'saved':
                self.webhook.set('')
                self.win.destroy()
                self.closed = True
                if self.on_complete:
                    self.on_complete()
                return
            if self.resolved:
                self.save_button.config(state='normal')
        self.win.after(100, self.pump)

    def close(self):
        if self.busy:
            self.status.set('操作正在进行，请等待完成后再关闭。')
            return
        self.webhook.set('')
        self.closed = True
        self.win.destroy()
