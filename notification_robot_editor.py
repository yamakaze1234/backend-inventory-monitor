"""机器人配置窗口：Webhook 与企业机器人共用通知范围，网络查询放到后台。"""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from inventory_theme import BG, INK, MUTED, button, wrap_label
from notification_enterprise import EnterpriseClient

MODES = {'企业机器人（钉钉账号）': 'enterprise', 'Webhook 机器人': 'webhook'}


class RobotEditor(tk.Toplevel):
    def __init__(self, panel, robot=None, client=None):
        super().__init__(panel)
        self.panel, self.robot = panel, robot
        self.client = client or EnterpriseClient()
        self.results = queue.SimpleQueue()
        self.busy = False
        self.closed = threading.Event()
        self.profiles, self.groups, self.bots = [], [], []
        self.title('编辑机器人' if robot else '添加机器人')
        self.configure(bg=BG)
        self.transient(panel.winfo_toplevel())
        self.resizable(True, False)
        self.protocol('WM_DELETE_WINDOW', self.close)
        body = tk.Frame(self, bg='white', padx=22, pady=18)
        body.pack(fill='both', expand=True, padx=14, pady=14)
        self.name = tk.StringVar(self, robot['name'] if robot else '')
        self.mode = tk.StringVar(self, next(k for k,v in MODES.items() if v == (robot or {}).get('mode','enterprise')))
        # 新机器人一律暂停，保存配置本身不会开启发送。
        self.enabled = tk.BooleanVar(self, (robot or {}).get('enabled', False))
        self.webhook = tk.StringVar(self)
        self.error = tk.StringVar(self)
        self.label(body, '机器人类型')
        self.mode_box = ttk.Combobox(body, textvariable=self.mode, values=list(MODES), state='disabled' if robot else 'readonly', width=52)
        self.mode_box.pack(fill='x', pady=(5,8))
        self.mode_box.bind('<<ComboboxSelected>>', lambda e:self.render_mode())
        self.label(body, '备注名字（方便辨认）')
        ttk.Entry(body, textvariable=self.name).pack(fill='x', pady=(5,8))
        self.destination = tk.Frame(body, bg='white'); self.destination.pack(fill='x')
        self.enterprise_frame = tk.Frame(self.destination, bg='white')
        self.label(self.enterprise_frame, '钉钉账号')
        self.profile_box = ttk.Combobox(self.enterprise_frame, state='readonly')
        self.profile_box.pack(fill='x', pady=(5,8))
        self.profile_box.bind('<<ComboboxSelected>>', lambda e:self.clear_groups())
        line = tk.Frame(self.enterprise_frame, bg='white'); line.pack(fill='x')
        self.query = tk.StringVar(self, ((robot or {}).get('enterprise') or {}).get('group_name',''))
        ttk.Entry(line, textvariable=self.query).pack(side='left', fill='x', expand=True)
        self.search_button = button(line, '查找群', self.search)
        self.search_button.pack(side='right', padx=(8,0))
        self.group_box = ttk.Combobox(self.enterprise_frame, state='readonly')
        self.group_box.pack(fill='x', pady=(5,8))
        self.group_box.bind('<<ComboboxSelected>>', lambda e:self.load_bots())
        self.label(self.enterprise_frame, '群内企业机器人')
        self.bot_box = ttk.Combobox(self.enterprise_frame, state='readonly')
        self.bot_box.pack(fill='x', pady=(5,8))
        self.bot_box.bind('<<ComboboxSelected>>', lambda e:self.name_bot())
        self.label(self.enterprise_frame, '使用本机已登录的钉钉账号，无需 Webhook。\n若列表为空，请先在钉钉监控设置中登录账号。', muted=True)
        self.webhook_frame = tk.Frame(self.destination, bg='white')
        self.label(self.webhook_frame, 'Webhook（留空保留原地址）' if robot else '目标群机器人的 Webhook')
        ttk.Entry(self.webhook_frame, textvariable=self.webhook, show='●').pack(fill='x', pady=(5,8))
        self.label(self.webhook_frame, '地址在本机加密保存，当前不支持加签模式。\n安全关键词按通知设置：库存、货源情况、价格调整。', muted=True)
        self.label(body, '通知类型', pady=(10,3))
        from notification_robot_ui import CHANNELS
        self.selected = {}
        for key,title in CHANNELS.items():
            self.selected[key] = tk.BooleanVar(self, key in (robot or {}).get('channels',['douyin_inventory']))
            ttk.Checkbutton(body, text=title, variable=self.selected[key]).pack(anchor='w')
        ttk.Checkbutton(body, text='启用此机器人（新添加默认暂停）', variable=self.enabled).pack(anchor='w', pady=(6,0))
        error = tk.Label(body, textvariable=self.error, bg='white', fg='#B42332', justify='left', anchor='w')
        error.pack(fill='x', pady=(8,6)); wrap_label(error)
        actions = tk.Frame(body, bg='white'); actions.pack(fill='x')
        self.save_button = button(actions, '保存', self.save, primary=True); self.save_button.pack(side='right')
        button(actions, '取消', self.close).pack(side='right', padx=8)
        self.render_mode()
        self.poll_id = self.after(80, self.poll)

    @staticmethod
    def label(parent, text, muted=False, pady=0):
        label = tk.Label(parent, text=text, bg='white', fg=MUTED if muted else INK, justify='left', anchor='w')
        label.pack(fill='x', pady=pady)
        wrap_label(label)

    def controls(self):
        self.mode_box.configure(state='disabled' if self.busy or self.robot else 'readonly')
        for box in (self.profile_box, self.group_box, self.bot_box):
            box.configure(state='disabled' if self.busy else 'readonly')
        for control in (self.search_button, self.save_button):
            control.configure(state='disabled' if self.busy else 'normal')

    def background(self, work, done):
        if self.busy:
            return
        self.busy = True; self.controls(); self.error.set('正在读取钉钉…')
        def run():
            try:
                self.results.put((done, work(), None))
            except ValueError as error:
                self.results.put((done, None, str(error)))
            except Exception:
                self.results.put((done, None, '操作未完成，请检查钉钉登录和本机配置权限后重试。'))
        threading.Thread(target=run, daemon=True, name='robot-config').start()

    def poll(self):
        # 后台只向队列交付结果；所有 Tk 控件都由主线程更新。
        while not self.results.empty():
            done, value, error = self.results.get()
            self.busy = False; self.controls(); self.error.set(error or '')
            if not error:
                done(value)
            if not self.winfo_exists():
                return
        self.poll_id = self.after(80, self.poll)

    def render_mode(self):
        enterprise = MODES[self.mode.get()] == 'enterprise'
        self.webhook_frame.pack_forget(); self.enterprise_frame.pack_forget()
        (self.enterprise_frame if enterprise else self.webhook_frame).pack(fill='x')
        if enterprise and not self.profiles:
            self.background(self.client.profiles, self.loaded_profiles)

    def loaded_profiles(self, values):
        self.profiles = values
        self.profile_box.configure(values=[f"{p['label']} ({p['profile']})" for p in values])
        existing = (self.robot or {}).get('enterprise')
        selected = [i for i,p in enumerate(values) if p['profile'] == existing['profile']] if existing else [i for i,p in enumerate(values) if p['current']]
        if len(selected) == 1:
            self.profile_box.current(selected[0])
            if existing:
                self.groups = [existing]; self.bots = [existing]
                self.group_box.configure(values=[existing['group_name']]); self.group_box.current(0)
                self.bot_box.configure(values=[existing['robot_name']]); self.bot_box.current(0)
        else:
            self.error.set('请选择本次使用的钉钉账号。' if values else '未发现已登录的钉钉账号，请先登录后重新打开窗口。')

    def clear_groups(self):
        self.groups, self.bots = [], []
        for box in (self.group_box, self.bot_box):
            box.set(''); box.configure(values=[])

    def profile(self):
        index = self.profile_box.current()
        if index < 0:
            raise ValueError('请先选择钉钉账号。')
        return self.profiles[index]['profile']

    def search(self):
        try:
            profile = self.profile(); query = self.query.get().strip()
            if not query:
                raise ValueError('请输入群名称。')
        except ValueError as error:
            self.error.set(str(error)); return
        self.clear_groups()
        self.background(lambda:self.client.groups(profile, query), self.loaded_groups)

    def loaded_groups(self, values):
        self.groups = values
        self.group_box.configure(values=[f"{g['group_name']} ({g['group_id']})" for g in values])
        if len(values) == 1:
            self.group_box.current(0); self.load_bots()
        elif not values:
            self.error.set('没有找到群，请检查群名与所选账号。')
        else:
            self.error.set('找到多个群，请按群名称和群标识选择。')

    def load_bots(self):
        self.bots = []; self.bot_box.set(''); self.bot_box.configure(values=[])
        if self.group_box.current() < 0:
            return
        profile = self.profile(); group = self.groups[self.group_box.current()]['group_id']
        self.background(lambda:self.client.bots(profile, group), self.loaded_bots)

    def loaded_bots(self, values):
        self.bots = values
        self.bot_box.configure(values=[f"{b['robot_name']} ({b['robot_code']})" for b in values])
        if len(values) == 1:
            self.bot_box.current(0); self.name_bot()
        elif not values:
            self.error.set('该群没有可用的企业机器人，请先在钉钉群内添加。')

    def name_bot(self):
        if not self.name.get().strip() and self.bot_box.current() >= 0:
            self.name.set(self.bots[self.bot_box.current()]['robot_name'])

    def save(self):
        mode = MODES[self.mode.get()]
        args = dict(name=self.name.get().strip(), webhook=self.webhook.get().strip(),
                    channels=[c for c,v in self.selected.items() if v.get()], enabled=self.enabled.get(),
                    robot_id=self.robot['id'] if self.robot else None, mode=mode)
        if not args['name'] or not args['channels']:
            self.error.set('请填写备注名字，并至少选择一种通知类型。'); return
        if mode == 'enterprise':
            try:
                profile = self.profile()
                if self.group_box.current() < 0 or self.bot_box.current() < 0:
                    raise ValueError('请选择目标群和群内的企业机器人。')
                args['enterprise'] = {**self.groups[self.group_box.current()], **self.bots[self.bot_box.current()], 'profile':profile}
            except ValueError as error:
                self.error.set(str(error)); return
        def work():
            if mode == 'enterprise':
                args['enterprise'] = self.client.verify(args['enterprise'])
            if self.closed.is_set():
                return None
            return self.panel.store.save(**args)
        self.background(work, self.saved)

    def saved(self, ident):
        self.close(); self.panel.changed()

    def close(self):
        self.closed.set()
        if getattr(self, 'poll_id', None):
            self.after_cancel(self.poll_id)
        self.webhook.set(''); self.destroy()
