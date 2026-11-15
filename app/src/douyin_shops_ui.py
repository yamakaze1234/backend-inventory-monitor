"""多店铺管理：独立维护表、浏览器连接和运行状态，支持自定义增删。"""
from pathlib import Path
import queue
import tkinter as tk
from tkinter import ttk, messagebox

from douyin_maintenance import KNOWN_SHOPS
from douyin_restock import stamp
from inventory_theme import BG, INK, MUTED, button, ScrollBar, sync_tree


class ShopsDialog(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.runtime = owner, owner.runtime
        self.reply = None
        self.after_id = None
        self.title('抖店店铺管理')
        self.geometry('1060x570')
        self.minsize(850, 500)
        self.configure(bg=BG)
        self.transient(owner.winfo_toplevel())
        body = tk.Frame(self, bg=BG, padx=18, pady=16)
        body.pack(fill='both', expand=True)
        tk.Label(body, text='每店单独选择主机维护表和登录连接；最多同时检查 3 店，更多店铺自动排队。',
                 bg=BG, fg=INK, anchor='w').pack(fill='x')
        tk.Label(body, text='推荐独立店铺浏览器：自动隔离登录，无需扩展标识；也可沿用已有扩展连接。',
                 bg=BG, fg=MUTED, anchor='w').pack(fill='x', pady=(6, 12))
        line = tk.Frame(body, bg=BG)
        line.pack(fill='x', pady=(0, 12))
        self.name = tk.StringVar(self)
        ttk.Combobox(line, textvariable=self.name, values=list(KNOWN_SHOPS), width=32).pack(side='left', fill='x', expand=True)
        button(line, '添加店铺', self.add, primary=True).pack(side='left', padx=8)
        button(line, '检查勾选店铺', self.check_all).pack(side='left')
        area = tk.Frame(body, bg='white')
        area.pack(fill='both', expand=True)
        columns = [('name','店铺',170), ('browser','登录设置',160), ('book','主机维护表',180),
                   ('state','当前状态',220), ('checked','最近库存快照',150)]
        self.tree = ttk.Treeview(area, columns=[c[0] for c in columns], show='headings', selectmode='browse', height=6)
        for key, title, width in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=100, stretch=key in ('name','state','book'))
        vertical = ScrollBar(area, command=self.tree.yview)
        horizontal = ScrollBar(area, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky='nsew'); vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        area.rowconfigure(0, weight=1); area.columnconfigure(0, weight=1)
        # 先预留底部操作区，避免全局表格行高较大时把配置和删除按钮挤出窗口。
        actions = tk.Frame(body, bg=BG); actions.pack(side='bottom', fill='x', pady=(14, 0), before=area)
        self.actions = actions
        from douyin_settings_ui import maintenance_dialog, login_dialog
        for label, command in [('主机维护表', lambda: maintenance_dialog(self)),
                               ('登录设置', lambda: login_dialog(self)),
                               ('连接店铺', lambda: self.action('login')),
                               ('确认并检查', lambda: self.action('confirm')),
                               ('查看库存', self.select_owner), ('删除店铺', self.remove)]:
            button(actions, label, command, padx=9).pack(side='left', padx=(0, 7))
        self.hint = tk.StringVar(self, '选中店铺后配置；新增店铺默认暂停，群自动发送关闭。')
        tk.Label(body, textvariable=self.hint, bg=BG, fg=MUTED, anchor='w', wraplength=970, justify='left').pack(side='bottom', fill='x', pady=12, before=actions)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh()
        if owner.ident and self.tree.exists(owner.ident):
            self.tree.selection_set(owner.ident)

    def config(self):
        selected = self.tree.selection()
        if not selected:
            self.hint.set('请先选择一个店铺。')
            return None
        config = self.runtime.store.store(selected[0])
        return None if config.get('deleted') else config

    def add(self):
        try:
            ident = self.runtime.store.add_store(self.name.get())
        except ValueError as error:
            self.hint.set(str(error)); return
        self.name.set('')
        self.refresh()
        self.tree.selection_set(ident)
        self.hint.set('店铺已添加，请选择维护表，再打开本店浏览器登录并校验连接。')

    def check_all(self):
        result = self.runtime.check_all()
        text = f"已排队 {result['queued']} 店，正在检查 {result['busy']} 店。"
        if result['skipped']:
            text += '未就绪已跳过：' + '、'.join(result['skipped'])
        self.hint.set(text)

    def action(self, operation):
        config = self.config()
        if not config:
            return
        if operation == 'confirm':
            if config['confirmed']:
                operation = 'check'
            elif not messagebox.askyesno('确认店铺身份', f"确认所选浏览器登录的是“{config['name']}”？\n确认后读取官方导出表，不发送群消息。", parent=self):
                return
        self.runtime.submit(operation, config['id'])
        self.hint.set('操作已排队，本店状态会自动更新。')

    def select_owner(self):
        config = self.config()
        if config:
            self.owner.ident = config['id']
            self.owner.cache_key = None
            self.owner.refresh_stores(); self.owner.refresh_details()
            self.close()

    def remove(self):
        config = self.config()
        if not config or self.reply is not None:
            return
        if not messagebox.askyesno('删除店铺监控', f"将“{config['name']}”移出监控，并同步移除在线提醒记录？\n本店当前任务结束后执行，历史导出文件保留。", parent=self):
            return
        self.reply = self.runtime.submit_remove(config['id'])
        self.hint.set('删除操作已排队，等待本店当前任务结束。')

    def refresh(self):
        if self.after_id:
            self.after_cancel(self.after_id)
        rows = []
        for config in self.runtime.store.stores():
            browser = ('Edge' if config['browser'] == 'edge' else 'Chrome') + ' · ' + ('独立店铺浏览器' if config.get('connection_mode') == 'managed' else config['browser_profile'] or '待选择连接')
            book = Path(config['maintenance_path']).name if config['maintenance_path'] else '待选择文件'
            state = self.runtime.activity(config['id']) or config['status']
            rows.append((config['id'], (config['name'], browser, book, state, stamp(config['checked'])), ()))
        sync_tree(self.tree, rows)
        if self.reply is not None:
            try:
                result = self.reply.get_nowait()
            except queue.Empty:
                pass
            else:
                self.reply = None
                self.hint.set('已移出监控，在线明细正在同步；结果可在抖店同步状态查看。' if result['ok'] else result['message'])
                self.owner.refresh_stores(); self.owner.refresh_details()
        self.after_id = self.after(1000, self.refresh)

    def close(self):
        if self.after_id:
            self.after_cancel(self.after_id)
            self.after_id = None
        self.destroy()
