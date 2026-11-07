"""Review and check watched products before removing a batch."""
import json
import tkinter as tk
from tkinter import ttk, messagebox

from inventory_theme import BG, INK, MUTED, ScrollBar, button, install_theme, wrap_label


class RemoveWindow(tk.Toplevel):
    def __init__(self, master, service, on_removed=None):
        super().__init__(master)
        self.service = service
        self.on_removed = on_removed or (lambda results: None)
        self.products, self.checked = {}, set()
        self.title('批量删除关注')
        self.configure(bg=BG)
        self.geometry(f'{min(1040, self.winfo_screenwidth()-60)}x{min(680, self.winfo_screenheight()-90)}')
        self.minsize(min(800, self.winfo_screenwidth()-60), min(480, self.winfo_screenheight()-90))
        install_theme(self)
        body = tk.Frame(self, bg=BG, padx=20, pady=16)
        body.pack(fill='both', expand=True)
        tk.Label(body, text='选择要删除关注的产品', font=('Microsoft YaHei UI', 17, 'bold'), fg=INK, bg=BG).pack(anchor='w')
        tk.Label(body, text='勾选产品或点击全选，再确认删除。删除关注后停止监控，历史库存和通知记录保留。',
                 bg=BG, fg=MUTED, anchor='w').pack(fill='x', pady=(6, 12))
        bar = tk.Frame(body, bg=BG)
        bar.pack(fill='x', pady=(0, 10))
        button(bar, '全选', self.select_all).pack(side='left')
        button(bar, '取消全选', self.clear_all).pack(side='left', padx=8)
        button(bar, '刷新清单', self.reload).pack(side='left')
        self.summary = tk.StringVar(self)
        tk.Label(bar, textvariable=self.summary, bg=BG, fg=INK).pack(side='right')
        footer = tk.Frame(body, bg=BG)
        footer.pack(side='bottom', fill='x', pady=(12, 0))
        self.delete_button = button(footer, '删除已勾选（0）', self.confirm)
        self.delete_button.configure(fg='#B42332', state='disabled')
        self.delete_button.pack(side='right')
        button(footer, '关闭', self.destroy).pack(side='right', padx=8)
        self.notice = tk.StringVar(self, '点击行内复选框勾选，也可选中一行后按空格。')
        note = tk.Label(body, textvariable=self.notice, bg=BG, fg=MUTED, anchor='w', justify='left')
        note.pack(side='bottom', fill='x', pady=(8, 0))
        wrap_label(note)
        table = tk.Frame(body, bg='white')
        table.pack(fill='both', expand=True)
        columns = ('checked', 'name', 'goods_id', 'threshold', 'basis', 'enabled')
        self.tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='browse', height=5)
        for key, label, width in zip(columns, ('勾选', '商品名称', 'goodsid', '预警值', '预警依据', '监控状态'),
                                     (55, 300, 100, 80, 175, 85)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=45, stretch=key in ('name', 'basis'),
                             anchor='w' if key in ('name', 'basis') else 'center')
        vertical = ScrollBar(table, command=self.tree.yview)
        horizontal = ScrollBar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.tag_configure('checked', background='#EDF3FF')
        self.tree.bind('<Button-1>', self.click_check)
        self.tree.bind('<space>', self.space_check)
        self.transient(master.winfo_toplevel())
        self.reload()

    def reload(self):
        # Keep the exact stored settings for the final concurrent-change check.
        with self.service.db() as db:
            self.products = {sku: json.loads(data) for sku, data in db.execute('SELECT sku,data FROM products ORDER BY rowid')}
        self.checked.clear()
        self.tree.delete(*self.tree.get_children())
        for sku, p in self.products.items():
            basis = p.get('warehouse_name', '分库') + ' · 库存' if p.get('stock_basis') == 'warehouse_stock' else '公司大库 · 可销'
            self.tree.insert('', 'end', iid=sku, values=('☐', p['name'], p.get('goods_id', ''), p['threshold'], basis,
                                                      '启用' if p.get('enabled', True) else '停用'))
        self.update_selection()
        self.notice.set('请勾选要删除关注的产品。' if self.products else '暂无关注产品。')

    def update_selection(self):
        self.summary.set(f'共 {len(self.products)} 个产品 · 已勾选 {len(self.checked)} 个')
        self.delete_button.configure(text=f'删除已勾选（{len(self.checked)}）', state='normal' if self.checked else 'disabled')
        for sku in self.products:
            self.tree.set(sku, 'checked', '☑' if sku in self.checked else '☐')
            self.tree.item(sku, tags=('checked',) if sku in self.checked else ())

    def toggle(self, sku):
        if sku not in self.products:
            return
        if sku in self.checked:
            self.checked.remove(sku)
        else:
            self.checked.add(sku)
        self.update_selection()

    def click_check(self, event):
        if self.tree.identify_region(event.x, event.y) == 'cell' and self.tree.identify_column(event.x) == '#1':
            self.toggle(self.tree.identify_row(event.y))

    def space_check(self, _=None):
        selected = self.tree.selection()
        if selected:
            self.toggle(selected[0])
        return 'break'

    def select_all(self):
        self.checked = set(self.products)
        self.update_selection()

    def clear_all(self):
        self.checked.clear()
        self.update_selection()

    def confirm(self):
        if not self.checked:
            return False
        chosen = [sku for sku in self.products if sku in self.checked]
        expected = {sku: self.products[sku] for sku in chosen}
        all_text = '全部 ' if len(chosen) == len(self.products) else ''
        if not messagebox.askyesno('确认删除关注',
                f'确认删除已勾选的{all_text}{len(chosen)} 个产品关注？\n\n这些产品将停止监控，历史库存和通知记录保留。', parent=self):
            return False
        try:
            results = self.service.remove_products(chosen, expected=expected)
        except ValueError as error:
            self.checked.clear()
            self.update_selection()
            self.notice.set(str(error))
            return False
        self.reload()
        text = f'已删除 {len(results)} 个产品关注。'
        if any(r['in_flight'] for r in results):
            text += ' 已开始发送的通知可能仍会送达。'
        self.notice.set(text)
        self.on_removed(results)
        return True
