"""按店铺维护下架链接排除名单；本地保存与在线明细同步分开展示。"""
import queue
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from douyin_exclusions import parse_products
from inventory_insight_ui import table
from inventory_theme import BG, INK, MUTED, button, sync_tree, wrap_label


class ExclusionsDialog(tk.Toplevel):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.config = panel.config()
        self.ident = self.config['id']
        self.pending, self.poll_id = None, None
        self.selected_url = panel.selected_product_url()
        self.title('下架链接管理 · ' + self.config['name'])
        self.configure(bg=BG); self.transient(panel.winfo_toplevel())
        self.geometry('850x650'); self.minsize(720, 580)
        self.protocol('WM_DELETE_WINDOW', self.close)
        body = tk.Frame(self, bg='white', padx=20, pady=16); body.pack(fill='both', expand=True, padx=12, pady=12)
        tk.Label(body, text=self.config['name'], bg='white', fg=INK, font=('Microsoft YaHei UI',11,'bold')).pack(anchor='w')
        label = tk.Label(body, text='加入名单后，本店该链接的全部 SKU 不再进入补库存提醒，在线明细会删除对应记录。\n此处仅管理提醒排除；商品重新上架后，请手动移出名单。', bg='white', fg=MUTED, justify='left', anchor='w')
        label.pack(fill='x', pady=(6,12)); wrap_label(label)
        tk.Label(body, text='商品 ID 或完整商品详情链接（每行一项，也可用逗号分隔）', bg='white', fg=INK).pack(anchor='w')
        self.input = ScrolledText(body, height=4, wrap='word', font=('Microsoft YaHei UI',10), relief='solid', borderwidth=1)
        self.input.pack(fill='x', pady=(6,8))
        line = tk.Frame(body, bg='white'); line.pack(fill='x')
        tk.Label(line, text='备注', bg='white', fg=MUTED).pack(side='left', padx=(0,8))
        self.reason = tk.StringVar(self, '手动标记下架')
        ttk.Entry(line, textvariable=self.reason).pack(side='left', fill='x', expand=True, padx=(0,8))
        self.add_button = button(line, '加入排除名单', self.add, primary=True, padx=12, pady=6)
        self.add_button.pack(side='right')
        self.fill_button = button(body, '填入当前选中链接', self.fill_selection, padx=10, pady=5)
        self.fill_button.pack(anchor='w', pady=8)
        if not self.selected_url:
            self.fill_button.config(state='disabled')
        self.hint = tk.StringVar(self, '新导出不会清空名单；自动群发送保持关闭。')
        hint = tk.Label(body, textvariable=self.hint, bg='white', fg=MUTED, justify='left', anchor='w')
        hint.pack(fill='x', pady=(2,10)); wrap_label(hint)
        self.count = tk.StringVar(self)
        tk.Label(body, textvariable=self.count, bg='white', fg=INK, anchor='w').pack(fill='x', pady=(0,6))
        # 操作区先靠底部布局，让列表随窗口增高而扩展，窄屏保留横向滚动。
        footer = tk.Frame(body, bg='white'); footer.pack(side='bottom', fill='x', pady=(10,0))
        self.remove_button = button(footer, '移出排除名单', self.remove, padx=10, pady=6)
        self.remove_button.pack(side='left')
        button(footer, '复制商品 ID', self.copy, padx=10, pady=6).pack(side='left', padx=8)
        button(footer, '关闭', self.close, padx=12, pady=6).pack(side='right')
        self.tree = table(body, dict(product='商品 ID', name='商品名称', reason='备注'), (210,350,180), height=7)
        self.tree.configure(selectmode='extended')
        self.refresh()

    def fill_selection(self):
        if self.selected_url:
            self.input.insert('end', ('\n' if self.input.get('1.0','end-1c').strip() else '') + self.selected_url)

    def refresh(self):
        rows = self.panel.runtime.store.excluded_products(self.ident)
        sync_tree(self.tree, [(r['product_id'], (r['product_id'],r['product_name'],r['reason']), ()) for r in rows])
        self.count.set(f'本店已排除 {len(rows)} 个链接')

    def add(self):
        try:
            ids = parse_products(self.input.get('1.0', 'end-1c'))
            if len(self.reason.get().strip()) > 120:
                raise ValueError('备注最多 120 个字符。')
        except ValueError as error:
            self.hint.set(str(error)); return
        self.submit(ids, remove=False)

    def remove(self):
        ids = list(self.tree.selection())
        if not ids:
            self.hint.set('请先选择要移出的链接，可多选。'); return
        self.submit(ids, remove=True)

    def submit(self, ids, *, remove):
        if self.pending:
            return
        self.pending = self.panel.runtime.submit_exclusions(self.ident, '\n'.join(ids), remove=remove, reason=self.reason.get())
        self.add_button.config(state='disabled'); self.remove_button.config(state='disabled')
        self.hint.set('已排队保存，正在等待当前采集结束…')
        self.poll_id = self.after(100, self.poll)

    def poll(self):
        # 后台线程只操作队列，窗口关闭后不再访问 Tk 控件。
        try:
            result = self.pending.get_nowait()
        except queue.Empty:
            self.poll_id = self.after(100, self.poll); return
        self.pending = None; self.poll_id = None
        self.add_button.config(state='normal'); self.remove_button.config(state='normal')
        if not result['ok']:
            self.hint.set(result['message']); return
        action = '移出' if result['remove'] else '新增'
        self.hint.set(f"本地已保存：{action} {result['changed']} 个，本店共 {result['total']} 个。在线同步结果可在“补库存提醒”页查看。")
        if not result['remove']:
            self.input.delete('1.0','end')
        self.refresh(); self.panel.refresh_stores(); self.panel.refresh_details()

    def copy(self):
        ids = self.tree.selection()
        if ids:
            self.clipboard_clear(); self.clipboard_append('\n'.join(ids))
            self.hint.set(f'已复制 {len(ids)} 个完整商品 ID。')

    def close(self):
        if self.poll_id:
            self.after_cancel(self.poll_id)
        self.destroy()
