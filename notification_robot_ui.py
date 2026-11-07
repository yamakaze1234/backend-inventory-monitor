"""Shared robot management widgets. Only public metadata is loaded into the UI."""
import tkinter as tk
import time
from tkinter import ttk, messagebox

from inventory_theme import BG, INK, MUTED, button, install_theme, wrap_label, ScrollBar

CHANNELS = {'inventory': '库存提醒', 'douyin_inventory': '抖店补库存（自动群发送关闭）',
            'daily': '货源日报', 'messages': '货源消息即时转发', 'profit': 'ERP 单价累计涨跌 100 元提醒'}
STATUSES = {'pending': '待发送', 'sent': '已发送', 'failed': '发送失败', 'unknown': '结果待核实',
            'sending': '发送中', 'skipped': '已跳过', 'partial': '部分送达', 'cancelled': '已取消'}


class RobotPanel(tk.Frame):
    def __init__(self, master, root=None, *, store=None, on_change=None):
        super().__init__(master, bg='white')
        install_theme(self)
        if store is None:
            from notification_robots import RobotStore
            store = RobotStore(root)
        self.store, self.on_change = store, on_change
        self.hint = tk.StringVar(self)
        label = tk.Label(self, text='支持企业机器人和 Webhook。企业机器人选择钉钉账号、群和机器人；新配置默认暂停，抖店自动群发送保持关闭。', bg='white', fg=MUTED, justify='left', anchor='w')
        label.pack(fill='x', pady=(0, 8))
        wrap_label(label)
        actions = tk.Frame(self, bg='white')
        actions.pack(fill='x', pady=(0, 8))
        button(actions, '添加机器人', self.edit, primary=True).pack(side='left')
        button(actions, '刷新列表', self.refresh).pack(side='left', padx=8)
        self.rows = tk.Frame(self, bg='white')
        self.rows.pack(fill='x')
        tk.Label(self, textvariable=self.hint, bg='white', fg=MUTED, anchor='w', justify='left').pack(fill='x', pady=(8, 0))
        self.receipts = tk.Label(self, bg='white', fg=MUTED, anchor='w', justify='left')
        self.receipts.pack(fill='x', pady=(12, 0))
        wrap_label(self.receipts)
        self.refresh()

    def refresh(self):
        try:
            robots = self.store.list_robots()
        except Exception:
            self.hint.set('读取机器人配置失败，请检查本机配置权限。')
            return
        for widget in self.rows.winfo_children():
            widget.destroy()
        for robot in robots:
            row = tk.Frame(self.rows, bg='white', pady=7)
            row.pack(fill='x')
            row.columnconfigure(0, weight=1)
            title = robot['name'] + ('  · 企业机器人' if robot.get('mode') == 'enterprise' else '  · Webhook') + ('  · 已启用' if robot.get('enabled', True) else '  · 已暂停')
            label = tk.Label(row, text=title, bg='white', fg=INK, anchor='w', justify='left')
            label.grid(row=0, column=0, sticky='ew')
            wrap_label(label)
            channels = '、'.join(CHANNELS[c] for c in robot.get('channels', []) if c in CHANNELS) or '未选择通知类型'
            label = tk.Label(row, text=channels, bg='white', fg=MUTED, anchor='w', justify='left')
            label.grid(row=1, column=0, sticky='ew')
            wrap_label(label)
            for column, (text, command) in enumerate([
                ('暂停' if robot.get('enabled', True) else '恢复', lambda r=robot: self.toggle(r)),
                ('编辑', lambda r=robot: self.edit(r)),
                ('删除', lambda r=robot: self.remove(r)),
            ], 1):
                button(row, text, command, padx=10, pady=6).grid(row=0, column=column, rowspan=2, padx=(6, 0))
        self.hint.set(f'共 {len(robots)} 个机器人。配置保存后生效；暂停仅停止该机器人的通知；恢复后只收新消息。' if robots else '还没有机器人，点击“添加机器人”开始配置。')
        try:
            receipts = self.store.recent_receipts(limit=8)
            lines = []
            for receipt in receipts:
                stamp = time.strftime('%m-%d %H:%M', time.localtime(float(receipt.get('at') or 0)))
                lines.append(f"{stamp}  {receipt.get('name', '机器人')} · {CHANNELS.get(receipt.get('channel'), '通知')} · {STATUSES.get(receipt.get('status'), '结果待核实')}")
            self.receipts.configure(text='最近投递（点击刷新列表更新）\n' + ('\n'.join(lines) or '暂无投递记录。'))
        except Exception:
            self.receipts.configure(text='最近投递暂时无法读取。')

    def changed(self):
        self.refresh()
        if self.on_change:
            self.on_change()

    def toggle(self, robot):
        try:
            self.store.set_enabled(robot['id'], not robot.get('enabled', True))
        except Exception:
            self.hint.set('未能更改状态，请检查本机配置权限。')
            return
        self.changed()

    def remove(self, robot):
        if not messagebox.askyesno('删除机器人配置', f'删除“{robot["name"]}”的本机配置？之后将不再向它发送通知。', parent=self):
            return
        try:
            self.store.delete(robot['id'])
        except Exception:
            self.hint.set('未能删除配置，请检查本机配置权限。')
            return
        self.changed()

    def edit(self, robot=None):
        # 企业机器人与 Webhook 使用独立目标表单，编辑时不加载任何凭据。
        from notification_robot_editor import RobotEditor
        return RobotEditor(self, robot)


def build_robot_page(parent, store):
    """左侧独立功能页沿用现有机器人配置，长列表在页面内滚动。"""
    from inventory_theme import RoundedCard, bind_wheel
    page = RoundedCard(parent)
    canvas = tk.Canvas(page.content, bg='white', highlightthickness=0)
    scrollbar = ScrollBar(page.content, command=canvas.yview)
    scrollbar.pack(side='right', fill='y')
    canvas.pack(fill='both', expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    panel = RobotPanel(canvas, store=store)
    window = canvas.create_window((0, 0), window=panel, anchor='nw')
    canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
    panel.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    bind_wheel(canvas)
    return page, panel


def open_robot_manager(parent, root):
    win = tk.Toplevel(parent)
    win.title('通知机器人管理')
    win.geometry('850x570')
    win.minsize(670, 350)
    win.configure(bg=BG)
    win.transient(parent.winfo_toplevel())
    from inventory_theme import bind_wheel
    canvas = tk.Canvas(win, bg='white', highlightthickness=0)
    scrollbar = ScrollBar(win, command=canvas.yview)
    scrollbar.pack(side='right', fill='y')
    canvas.pack(fill='both', expand=True, padx=16, pady=16)
    canvas.configure(yscrollcommand=scrollbar.set)
    panel = RobotPanel(canvas, root)
    window = canvas.create_window((0, 0), window=panel, anchor='nw')
    canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
    panel.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    bind_wheel(canvas)
    return win
