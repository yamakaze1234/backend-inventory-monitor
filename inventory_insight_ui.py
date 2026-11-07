"""监控覆盖与受影响配置视图：列表优先，长说明只在选中后展开。"""
import csv
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from inventory_theme import BG, INK, MUTED, BLUE, RoundedCard, ScrollBar, button, sync_tree, wrap_label
from inventory_impact import InventoryImpact

HEALTH_LABELS = {'success': '已更新', 'isolated': '已隔离', 'missing': '商品未返回', 'warehouse_missing': '库房待核对'}


def table(parent, columns, widths, *, height=10):
    frame = tk.Frame(parent, bg='white')
    frame.pack(fill='both', expand=True)
    tree = ttk.Treeview(frame, columns=tuple(columns), show='headings', height=height, selectmode='browse')
    for (key, title), width in zip(columns.items(), widths):
        tree.heading(key, text=title, anchor='w')
        tree.column(key, width=width, minwidth=60, stretch=key in ('name', 'reason', 'parts'), anchor='w')
    vertical = ScrollBar(frame, command=tree.yview)
    horizontal = ScrollBar(frame, orient='horizontal', command=tree.xview)
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    tree.grid(row=0, column=0, sticky='nsew')
    vertical.grid(row=0, column=1, sticky='ns')
    horizontal.grid(row=1, column=0, sticky='ew')
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    tree.tag_configure('warning', background='#FFF6E8', foreground='#8A4B0F')
    tree.tag_configure('muted', foreground=MUTED)
    return tree


def show_coverage(parent, service, selected='all'):
    status = service.status()
    coverage = status.get('coverage', {})
    window = tk.Toplevel(parent)
    window.title('本轮检查明细')
    window.geometry('900x480')
    window.minsize(650, 350)
    window.configure(bg=BG)
    outer = RoundedCard(window)
    outer.pack(fill='both', expand=True, padx=16, pady=16)
    content = outer.content
    checked = coverage.get('checked_at')
    text = '尚未完成一轮库存检查' if not checked else '检查时间：' + time.strftime('%m-%d %H:%M:%S', time.localtime(checked))
    if not status.get('coverage_current', True):
        text += ' · 以下为上轮结果，请结合当前监控状态核对'
    if status.get('connection') != 'online':
        text += ' · 当前数据尚未确认'
    tk.Label(content, text=text, bg='white', fg=INK, anchor='w').pack(fill='x', pady=(0, 12))
    choice = tk.StringVar(window, '全部' if selected == 'all' else HEALTH_LABELS[selected])
    combo = ttk.Combobox(content, textvariable=choice, values=['全部'] + list(HEALTH_LABELS.values()), state='readonly')
    combo.pack(anchor='w', pady=(0, 12))
    tree = table(content, dict(name='商品', gid='ERP 编号', warehouse='库房', state='检查结果', last='上次有效检查', reason='原因'), (180, 90, 100, 100, 145, 260))
    last = {p['sku']: p.get('observed_at', 0) for p in status['products']}
    def refresh(*_):
        rows = []
        for item in coverage.get('items', []):
            state = HEALTH_LABELS[item['state']]
            if choice.get() not in ('全部', state):
                continue
            when = coverage['checked_at'] if item['state'] == 'success' else last.get(item['sku'], 0)
            stamp = time.strftime('%m-%d %H:%M:%S', time.localtime(when)) if when else '尚无有效库存'
            rows.append((item['sku'], (item['name'], item['goods_id'], item['warehouse'], state, stamp, item['reason']),
                         () if item['state'] == 'success' else ('warning',)))
        sync_tree(tree, rows)
    combo.bind('<<ComboboxSelected>>', refresh)
    refresh()
    return window


class ImpactPanel(tk.Frame):
    def __init__(self, parent, service):
        super().__init__(parent, bg=BG)
        self.model, self.timer = InventoryImpact(service), None
        self.rows = {}
        card = RoundedCard(self)
        card.pack(fill='both', expand=True)
        body = card.content
        bar = tk.Frame(body, bg='white')
        bar.pack(fill='x', pady=(0, 12))
        button(bar, '导入配置快照', self.import_manifest, True).pack(side='left')
        button(bar, '更新已导入快照', self.reload_manifest).pack(side='left', padx=8)
        button(bar, '导出清单', self.export).pack(side='right')
        self.summary = tk.StringVar(self)
        tk.Label(body, textvariable=self.summary, bg='white', fg=INK, anchor='w').pack(fill='x')
        self.note = tk.StringVar(self)
        label = tk.Label(body, textvariable=self.note, bg='white', fg=MUTED, anchor='w', justify='left')
        label.pack(fill='x', pady=(6, 12))
        wrap_label(label)
        filters = tk.Frame(body, bg='white')
        filters.pack(fill='x', pady=(0, 10))
        self.query = tk.StringVar(self)
        self.filter = tk.StringVar(self, '全部待核对')
        ttk.Entry(filters, textvariable=self.query).pack(side='left', fill='x', expand=True)
        ttk.Combobox(filters, textvariable=self.filter, values=('全部待核对', '有低库存配件', '优先核对', '关联不完整'), state='readonly', width=16).pack(side='left', padx=10)
        button(filters, '关联所选商品', self.bind_listing).pack(side='right')
        self.tree = table(body, dict(priority='处理顺序', shop='店铺', spu='SPU', name='商品 / 配置', parts='涉及配件', listing='平台库存'), (95, 115, 60, 240, 220, 155))
        from tkinter.scrolledtext import ScrolledText
        self.detail = ScrolledText(body, height=4, wrap='word', bg='#F5F7FA', fg=INK, relief='flat', padx=12, pady=10, state='disabled')
        self.detail.pack(side='bottom', fill='x', pady=(8, 0), before=self.tree.master)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        footer = tk.Frame(body, bg='white')
        footer.pack(side='bottom', fill='x', pady=(8, 0), before=self.detail)
        self.notice = tk.StringVar(self)
        tk.Label(footer, textvariable=self.notice, bg='white', fg='#8A4B0F', anchor='w').pack(side='left', fill='x', expand=True)
        button(footer, '解除配置关联', self.unbind).pack(side='right')
        for var in (self.query, self.filter):
            var.trace_add('write', lambda *_: self.refresh())
        self.refresh()

    def import_manifest(self):
        path = filedialog.askopenfilename(parent=self, title='选择工作台发布的完整配置快照', filetypes=[('JSON 配置快照', '*.json')])
        if path:
            self.load(path)

    def load(self, path):
        try:
            count = self.model.import_manifest(path)
            self.notice.set(f'已导入 {count} 个配置。')
        except (OSError, ValueError) as error:
            self.notice.set(str(error))
        self.refresh()

    def reload_manifest(self):
        with self.model.service.db() as db:
            path = self.model.service.get(db, 'impact_path')
        if path:
            self.load(path)
        else:
            self.import_manifest()

    def refresh(self):
        if self.timer:
            self.after_cancel(self.timer)
        try:
            report = self.model.report()
            self.summary.set(report['summary'])
            notes = '仅按精确 ERP 商品编号关联。SPU、店铺和 SKU 未关联时列为待核对。'
            if report.get('published_at'):
                # 操作页面统一显示北京时间，不暴露原始 UTC / ISO 字符串。
                try:
                    published = datetime.fromisoformat(report['published_at'].replace('Z', '+00:00'))
                    stamp = published.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                except (ValueError, TypeError):
                    stamp = '时间待核对'
                notes = f'配置发布：{stamp} · ' + notes
            if report.get('bound') and not report.get('fresh'):
                notes = '配置快照已超过 24 小时或发布时间异常，请更新后核对。' + notes
            if report.get('bound') and not report.get('scope_matches'):
                notes = 'ERP 来源已变化，原关联仅供核对。' + notes
            self.note.set(notes)
            rows = report['rows']
            query = self.query.get().strip().casefold()
            rows = [r for r in rows if query in ' '.join([r['shop_id'], r['spu'], r['product'], r['name']] + [p['name'] for p in r['parts']]).casefold()]
            if self.filter.get() == '优先核对':
                rows = [r for r in rows if r['priority'] == '优先核对']
            elif self.filter.get() == '有低库存配件':
                rows = [r for r in rows if r['parts']]
            elif self.filter.get() == '关联不完整':
                rows = [r for r in rows if not r['mapping'] or r['uncertain']]
            self.rows = {str((r['shop_id'], r['config_id'])): r for r in rows}
            sync_tree(self.tree, [(key, (r['priority'], r['shop_id'], r['spu'] or '待关联', f"{r['product']} / {r['name']}",
                '；'.join(p['name'] for p in r['parts']) or '库存待核对', r['listing']), ('warning',) if r['priority']=='优先核对' else ()) for key, r in self.rows.items()])
            self.select()
        except (ValueError, OSError, sqlite3.Error) as error:
            self.summary.set('配置关联暂不可读，请检查导入文件或本地库存。')
            self.notice.set(str(error))
        self.timer = self.after(5000, self.refresh)

    def selected(self):
        return self.rows.get(next(iter(self.tree.selection()), ''))

    def select(self, *_):
        row = self.selected()
        text = '选择一条配置，查看缺货配件、库存口径和待核对原因。'
        if row:
            text = f"{row['shop_id']} / {row['product']} / {row['name']}\n"
            text += '\n'.join(f"{p['name']}（{p['goods_id']}）· {p['warehouse']} · 数量 {p['quantity']} / 预警 {p['threshold']} · 每套用量 {p['qty_per_config'] or '待核对'}" for p in row['parts'])
            text += '\n' + '\n'.join(row['uncertain'])
            if row['mapping']:
                text += f"\n抖店商品 ID：{row['mapping']['product_id']} · SKU ID：{row['mapping']['sku_id']}"
        if getattr(self, '_detail_text', None) == text.strip():
            return
        self._detail_text = text.strip()
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text.strip())
        self.detail.configure(state='disabled')

    def bind_listing(self):
        row = self.selected()
        if not row:
            self.notice.set('先选择一条配置。')
            return
        stores, _ = self.model.shop_data()
        stores = [s for s in stores if s['confirmed']]
        if not stores:
            self.notice.set('请先到“抖店库存”添加店铺并核验身份。')
            return
        window = tk.Toplevel(self)
        window.title('关联明确的商品和 SKU')
        window.geometry('510x370')
        form = tk.Frame(window, padx=24, pady=20)
        form.pack(fill='both', expand=True)
        tk.Label(form, text=f"{row['shop_id']} · {row['name']}", wraplength=450).pack(anchor='w', pady=(0, 12))
        labels = {f"{s['name']} · {s['id'][:8]}": s['id'] for s in stores}
        choice = tk.StringVar(window)
        ttk.Combobox(form, textvariable=choice, values=list(labels), state='readonly').pack(fill='x')
        fields = {}
        for key, title in [('spu', 'SPU 编号'), ('product_id', '抖店商品 ID'), ('sku_id', '抖店 SKU ID')]:
            tk.Label(form, text=title).pack(anchor='w', pady=(8, 2))
            fields[key] = tk.StringVar(window, (row.get('mapping') or {}).get(key, ''))
            ttk.Entry(form, textvariable=fields[key]).pack(fill='x')
        def save():
            try:
                self.model.bind_listing(row['shop_id'], row['config_id'], store_id=labels.get(choice.get()), **{k:v.get().strip() for k,v in fields.items()})
            except ValueError as error:
                messagebox.showerror('未保存关联', str(error), parent=window)
                return
            window.destroy()
            self.refresh()
        button(form, '保存关联', save, True).pack(anchor='e', pady=(16, 0))

    def unbind(self):
        if messagebox.askyesno('解除配置关联', '解除本功能导入的配置及商品关联？原配置文件和库存记录会保留。', parent=self):
            self.model.unbind()
            self.refresh()

    def export(self):
        path = filedialog.asksaveasfilename(parent=self, initialfile='受影响配置.csv', defaultextension='.csv')
        if not path:
            return
        with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['处理顺序', '店铺', 'SPU', '商品', '配置', '配件', '平台库存', '待核对项'])
            # 导出内容作为文本，避免名称中的等号在表格软件中成为公式。
            for row in self.rows.values():
                cells = [row['priority'], row['shop_id'], row['spu'], row['product'], row['name'],
                         '；'.join(p['name'] for p in row['parts']), row['listing'], '；'.join(row['uncertain'])]
                writer.writerow(["'" + str(c) if str(c).startswith(('=', '+', '-', '@')) else c for c in cells])
        self.notice.set(f'已导出 {len(self.rows)} 条。')

    def destroy(self):
        if self.timer:
            self.after_cancel(self.timer)
        super().destroy()
