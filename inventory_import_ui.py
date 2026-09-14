"""Product-sheet review window. Nothing is saved until the explicit final click."""
import time
import tkinter as tk
from tkinter import ttk, filedialog

from inventory_import import read_sheet, write_template, prepare_import, confirm_import
from inventory_theme import BG, INK, MUTED, ScrollBar, button, wrap_label, install_theme


class ImportWindow(tk.Toplevel):
    def __init__(self, master, service, on_saved=None):
        super().__init__(master)
        self.service, self.on_saved = service, on_saved or (lambda: None)
        self.inputs, self.plan = [], None
        self.row_dirty, self.loading_row = False, False
        self.title('表格导入 · 监控产品')
        self.configure(bg=BG)
        self.geometry(f'{min(1160, self.winfo_screenwidth()-60)}x{min(740, self.winfo_screenheight()-90)}')
        self.minsize(min(880, self.winfo_screenwidth()-60), min(590, self.winfo_screenheight()-90))
        install_theme(self)
        body = tk.Frame(self, bg=BG, padx=20, pady=16)
        body.pack(fill='both', expand=True)
        tk.Label(body, text='批量导入监控产品', bg=BG, fg=INK, font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w')
        help_text = '① 选择 Excel / CSV　→　② 核对总览　→　③ 确认设置\n填写 goodsid 或名称、预警值；goodsid 优先。统一使用公司大库可销数，不采用分库。'
        tk.Label(body, text=help_text, bg=BG, fg=MUTED, justify='left', font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(6, 12))
        bar = tk.Frame(body, bg=BG)
        bar.pack(fill='x')
        button(bar, '选择表格', self.choose_file, True).pack(side='left')
        button(bar, '保存空白模板', self.template).pack(side='left', padx=8)
        button(bar, '刷新总览', self.refresh_plan).pack(side='left')
        self.filename = tk.StringVar(self, '支持 .xlsx（首张工作表）或 .csv，最多 5000 行')
        tk.Label(body, textvariable=self.filename, bg=BG, fg=MUTED, anchor='w').pack(fill='x', pady=8)
        self.summary = tk.StringVar(self, '确认设置前不会添加或修改监控产品。')
        summary = tk.Label(body, textvariable=self.summary, bg=BG, fg=INK, anchor='w', justify='left')
        summary.pack(fill='x', pady=(0, 8))
        wrap_label(summary)
        table = tk.Frame(body, bg='white')
        table.pack(fill='both', expand=True)
        columns = ('line', 'action', 'goods_id', 'name', 'old', 'threshold', 'basis', 'status')
        self.tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='browse', height=4)
        for key, title, width in zip(columns,
                ('行', '操作', 'goodsid', '匹配商品 / 表中名称', '原预警值', '新预警值', '预警依据', '核对结果'),
                (42, 60, 100, 245, 80, 80, 140, 230)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, stretch=key in ('name', 'status'))
        vertical = ScrollBar(table, command=self.tree.yview)
        horizontal = ScrollBar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.tag_configure('error', foreground='#B42332')
        self.tree.tag_configure('excluded', foreground=MUTED)
        self.tree.bind('<<TreeviewSelect>>', self.select_row)
        editor = tk.LabelFrame(body, text='选中行后可修正匹配、预警值，或排除此行', bg=BG, fg=MUTED, padx=10, pady=8)
        editor.pack(fill='x', pady=10)
        self.candidate = tk.StringVar(self)
        self.candidates = ttk.Combobox(editor, textvariable=self.candidate, state='readonly')
        self.candidates.grid(row=0, column=0, sticky='ew', padx=(0, 10))
        self.threshold = tk.StringVar(self)
        self.threshold.trace_add('write', self.mark_dirty)
        self.candidate.trace_add('write', self.mark_dirty)
        ttk.Entry(editor, textvariable=self.threshold, width=10).grid(row=0, column=1, padx=(0, 8))
        button(editor, '应用到此行', self.apply_row).grid(row=0, column=2)
        button(editor, '排除 / 恢复此行', self.toggle_row).grid(row=0, column=3, padx=(8, 0))
        editor.columnconfigure(0, weight=1)
        self.details = tk.StringVar(self, '名称多候选时，请在下拉列表中选定实际商品。')
        detail = tk.Label(editor, textvariable=self.details, bg=BG, fg=MUTED, anchor='w', justify='left')
        detail.grid(row=1, column=0, columnspan=4, sticky='ew', pady=(6, 0))
        wrap_label(detail)
        self.notice = tk.StringVar(self)
        note = tk.Label(body, textvariable=self.notice, bg=BG, fg='#B42332', anchor='w', justify='left')
        note.pack(fill='x')
        wrap_label(note)
        footer = tk.Frame(body, bg=BG)
        footer.pack(fill='x', pady=(8, 0))
        self.confirm_button = button(footer, '确认设置', self.confirm, True)
        self.confirm_button.pack(side='right')
        self.confirm_button.configure(state='disabled')
        button(footer, '取消', self.destroy).pack(side='right', padx=8)
        tk.Label(footer, text='已有产品更新预警值与仓库依据；保留启停和提醒类型。', bg=BG, fg=MUTED).pack(side='left')
        # Reserve the actions first so a tall table cannot push confirmation off-screen.
        for widget in (table, editor, note, footer):
            widget.pack_forget()
        footer.pack(side='bottom', fill='x', pady=(8, 0))
        note.pack(side='bottom', fill='x')
        editor.pack(side='bottom', fill='x', pady=10)
        table.pack(fill='both', expand=True)
        self.transient(master.winfo_toplevel())

    def choose_file(self):
        path = filedialog.askopenfilename(parent=self, title='选择监控产品表格',
            filetypes=[('Excel / CSV', '*.xlsx *.csv'), ('Excel', '*.xlsx'), ('CSV', '*.csv')])
        if path:
            self.load_file(path)

    def load_file(self, path):
        # A failed new file must not leave the previous batch confirmable.
        self.inputs, self.plan = [], None
        self.confirm_button.configure(state='disabled')
        self.tree.delete(*self.tree.get_children())
        self.filename.set(str(path))
        try:
            self.inputs = read_sheet(path)
            self.refresh_plan()
        except Exception as error:
            self.summary.set('文件读取失败，尚未导入。')
            self.notice.set(str(error))

    def template(self):
        path = filedialog.asksaveasfilename(parent=self, title='保存空白导入模板',
            initialfile='监控产品导入模板.xlsx', defaultextension='.xlsx',
            filetypes=[('Excel', '*.xlsx'), ('CSV', '*.csv')])
        if path:
            try:
                write_template(path)
                self.notice.set('模板已保存。填写后点击“选择表格”；名称可以留空，建议使用 goodsid。')
            except Exception as error:
                self.notice.set(str(error))

    def refresh_plan(self):
        if not self.inputs:
            return
        selected = self.tree.selection()
        self.confirm_button.configure(state='disabled')
        try:
            self.plan = prepare_import(self.service, self.inputs)
        except ValueError as error:
            self.plan = None
            self.notice.set(str(error))
            return
        self.row_dirty = False
        self.tree.delete(*self.tree.get_children())
        self.notice.set('')
        rows = self.plan['rows']
        for index, row in enumerate(rows):
            product, previous = row['product'] or {}, row['previous'] or {}
            basis = '公司大库 · 可销'
            if previous.get('stock_basis') == 'warehouse_stock':
                basis = f"{previous.get('warehouse_name', '分库')} → 公司大库"
            self.tree.insert('', 'end', iid=str(index), values=(row['line'], row['action'] or '待处理',
                product.get('goods_id') or row.get('goods_id', ''), product.get('name') or row.get('name', ''),
                previous.get('threshold', '—'), row['threshold'], basis,
                row['problem'] or row['note'] or ('已排除' if row.get('excluded') else '匹配完成')),
                tags=('error',) if row['problem'] else ('excluded',) if row.get('excluded') else ())
        errors = sum(bool(r['problem']) for r in rows)
        count = sum(bool(r['product']) for r in rows)
        stamp = time.strftime('%m-%d %H:%M', time.localtime(self.plan['checked_at'])) if self.plan['checked_at'] else '尚未采集'
        self.summary.set(f"共 {len(rows)} 行 · 新增 {sum(r['action']=='新增' for r in rows)} · 更新 {sum(r['action']=='更新' for r in rows)} · 待处理 {errors} · 排除 {sum(bool(r.get('excluded')) for r in rows)}\nERP 目录采集时间：{stamp}。未匹配的产品请核对表格，或在 ERP 在线时完成检查后刷新总览。")
        self.confirm_button.configure(state='normal' if count and not errors else 'disabled')
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        self.select_row()

    def selected_index(self):
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def select_row(self, *_):
        self.loading_row = True
        try:
            self.load_selected_row()
        finally:
            self.loading_row = False

    def mark_dirty(self, *_):
        if not self.loading_row and hasattr(self, 'confirm_button'):
            self.row_dirty = True
            self.confirm_button.configure(state='disabled')
            self.notice.set('此行修改尚未应用，请点击“应用到此行”后核对总览。')

    def load_selected_row(self):
        index = self.selected_index()
        if index is None or not self.plan:
            self.candidates.configure(values=())
            self.candidate.set('')
            self.threshold.set('')
            return
        row = self.plan['rows'][index]
        options = [f"{p.get('goods_id', '')} · {p['name']} · {p['sku']}" for p in row['candidates']]
        self.candidates.configure(values=options)
        self.candidate.set('')
        chosen = (row['product'] or {}).get('sku') or row.get('selected_sku')
        for position, product in enumerate(row['candidates']):
            if product['sku'] == chosen:
                self.candidates.current(position)
        self.threshold.set(row['threshold'])
        self.details.set(f"第 {row['line']} 行 · " + (row['problem'] or row['note'] or '请核对商品和预警值'))

    def apply_row(self):
        index = self.selected_index()
        if index is None or not self.plan:
            return
        row = self.inputs[index]
        position = self.candidates.current()
        if position >= 0:
            row['selected_sku'] = self.plan['rows'][index]['candidates'][position]['sku']
        row['threshold'] = self.threshold.get().strip()
        self.refresh_plan()

    def toggle_row(self):
        index = self.selected_index()
        if index is not None:
            self.inputs[index]['excluded'] = not self.inputs[index].get('excluded')
            self.refresh_plan()

    def confirm(self):
        if not self.plan or self.row_dirty or str(self.confirm_button.cget('state')) == 'disabled':
            return False
        self.confirm_button.configure(state='disabled')
        try:
            count = confirm_import(self.service, self.plan)
        except ValueError as error:
            self.notice.set(str(error))
            return False
        self.inputs, self.plan = [], None
        self.tree.delete(*self.tree.get_children())
        self.summary.set(f'已确认设置 {count} 个产品，统一使用公司大库可销数。')
        self.notice.set('保存成功。后续 ERP 检查将按这些设置监控。')
        self.on_saved()
        return True
