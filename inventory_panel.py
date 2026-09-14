"""Local inventory configuration UI. All data access goes through InventoryService."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import time
import tkinter as tk
from tkinter import ttk

from inventory_theme import BG, INK, MUTED, BLUE, BORDER, ScrollBar, RoundedCard, bind_wheel, install_theme, section, wrap_label
from inventory_theme import button as themed_button
from inventory_theme import sync_tree
ALERTS = {'inbound': '入库 / 库存增加', 'pending_inbound': '待入库', 'low': '低库存', 'oversold': '超售', 'negative_stock': '负库存', 'recovery': '风险恢复'}
CATEGORIES = ('RTX 5070 及以上显卡', '其余显卡', '主板', 'CPU', '内存', '其他')


def window_dimensions(screen_width, screen_height, scale=1, extra_height=0):
    """Return physical pixels; Tk already scales point-based fonts itself."""
    available_width, available_height = max(1, screen_width - 60), max(1, screen_height - 90)
    width = min(round(1080 * scale), available_width)
    height = min(round((780 + extra_height) * scale), available_height)
    return width, height, min(860, width), min(620, height)


def recipient_summary(event):
    names = {'pending': '待发送', 'sent': '已发送', 'unknown': '待核实', 'failed': '失败待重试',
             'sending': '发送中', 'cancelled': '已取消', 'partial': '部分送达', 'skipped': '已跳过'}
    recipients = (event.get('delivery') or {}).get('recipients') or []
    return ('\n    ' + '；'.join(str(r.get('name', '机器人')) + '：' + names.get(r.get('status'), '待发送')
                              for r in recipients)) if recipients else ''


def delivery_summary(status):
    robot = status.get('robot_destination')
    if robot is not None:
        text = '库存通知：通知机器人 · ' + ('已配置' if robot.get('configured') else '待配置')
        if 'enabled_count' in robot:
            text += f" · {robot['enabled_count']} 个启用"
    else:
        destination = status.get('destination') or {}
        verified = bool(destination.get('group_id') and destination.get('verified_at'))
        text = '库存通知：' + (str(destination.get('name', destination.get('group_name', '接收群'))) + ' · 已核对' if verified else '指定群待核对')
    if status.get('delivery_error'):
        text += ' · ' + str(status['delivery_error'])
    for state, label in [('unknown', '待核实（不会自动重发）'), ('partial', '部分送达'), ('skipped', '已跳过')]:
        count = sum(event.get('delivery_status') == state for event in status.get('events', []))
        if count:
            text += f' · 最近记录中 {count} 条{label}'
    return text


def product_risk_label(product, connection):
    risk = product.get('risk') or 'unknown'
    if not product.get('enabled', True):
        risk = 'disabled'
    elif product.get('monitor_qty' if product.get('stock_basis') == 'warehouse_stock' else 'able') is None:
        risk = 'unknown'
    label = {'normal': '正常', 'low': '低库存', 'oversold': '超售', 'negative_stock': '负库存', 'unknown': '待检查', 'disabled': '已停用'}.get(risk, '待检查')
    if connection != 'online' and risk in ('normal', 'low', 'oversold', 'negative_stock'):
        return '上次' + label, 'unknown'
    return label, risk


@dataclass
class ProductDraft:
    sku: str = ''
    goods_id: str = ''
    name: str = ''
    category: str = '内存'
    threshold: str = ''
    keywords: str = ''
    enabled: bool = True
    stock_basis: str = 'company_able'
    warehouse_id: str = ''
    warehouse_name: str = ''
    alerts: dict = field(default_factory=lambda: dict.fromkeys(ALERTS, True))

    @classmethod
    def from_product(cls, product):
        draft = cls(**{key: product[key] for key in ('sku', 'goods_id', 'name', 'category', 'keywords', 'enabled', 'stock_basis', 'warehouse_id', 'warehouse_name') if key in product})
        draft.threshold = str(product.get('threshold', ''))
        draft.alerts.update(product.get('alerts', {}))
        return draft

    def suggested_threshold(self):
        return dict(zip(CATEGORIES, (5, 20, 10, 20, 10, None))).get(self.category)

    def payload(self):
        value = str(self.threshold).strip()
        if not value.isascii() or not value.isdecimal():
            raise ValueError('低库存预警数量必须是非负整数，可填写 0。')
        return {'sku': self.sku.strip(), 'goods_id': self.goods_id.strip(), 'name': self.name.strip(), 'category': self.category,
                'threshold': int(value), 'keywords': self.keywords.strip(),
                'enabled': self.enabled, 'alerts': dict(self.alerts), 'stock_basis': self.stock_basis,
                'warehouse_id': self.warehouse_id.strip(), 'warehouse_name': self.warehouse_name.strip()}


class InventoryPanel(tk.Frame):
    def __init__(self, master, service, *, on_hide=None, setup_path=None):
        super().__init__(master, bg=BG, padx=20, pady=15)
        install_theme(self)
        self.service = service
        self.on_hide = on_hide or (lambda: self.winfo_toplevel().withdraw())
        self.setup_path = Path(setup_path or Path(__file__).with_name('inventory-setup.md'))
        self.selected_sku = None
        self.rows = {}
        self._after = None
        self.vars = {key: tk.StringVar(self) for key in ('name', 'goods_id', 'sku', 'category', 'threshold', 'keywords', 'stock_basis', 'warehouse_id', 'warehouse_name')}
        self.enabled = tk.BooleanVar(self, True)
        self.alerts = {key: tk.BooleanVar(self, True) for key in ALERTS}
        self.search = tk.StringVar(self)
        self.notice = tk.StringVar(self)
        with service.db() as db:
            native_enabled = service.get(db, 'erp_native_enabled', False)
        self.native_enabled = tk.BooleanVar(self, native_enabled)
        self._build()
        self.search.trace_add('write', lambda *_: self.refresh())
        self.new_product(reveal=False)
        self.notice.set('')
        self.refresh()

    def label(self, parent, text='', **kw):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=kw.pop('fg', INK),
                        font=('Microsoft YaHei UI', kw.pop('size', 10)), **kw)

    def button(self, parent, text, command, primary=False):
        return themed_button(parent, text, command, primary)

    def _build(self):
        style = ttk.Style(self)
        style.configure('Inventory.Treeview', rowheight=54, font=('Microsoft YaHei UI', 10), background='white', fieldbackground='white')
        style.configure('Inventory.Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'))
        top = tk.Frame(self, bg=BG)
        top.pack(fill='x', pady=(0, 10))
        self.connection = self.label(top, '● 等待 ERP', size=11)
        self.connection.pack(side='left')
        self.message_state = self.label(top, '钉钉：请查看消息监控页', fg=MUTED)
        self.message_state.pack(side='right')
        self.delivery_state = self.label(self, fg=MUTED, anchor='w', wraplength=960, justify='left', size=9)
        self.delivery_state.pack(fill='x', pady=(0, 5))
        self.connection_detail = self.label(self, fg='#BA6415', anchor='w', wraplength=960, justify='left', size=9)
        self.connection_detail.pack(fill='x', pady=(0, 6))
        recovery_bar = tk.Frame(self, bg=BG)
        recovery_bar.pack(fill='x', pady=(0, 6))
        ttk.Style(self).configure('Recovery.TCheckbutton', background=BG, foreground=INK,
                                  font=('Microsoft YaHei UI', 9))
        ttk.Style(self).map('Recovery.TCheckbutton', background=[('active', BG), ('!active', BG)],
                           foreground=[('disabled', MUTED), ('!disabled', INK)])
        ttk.Checkbutton(recovery_bar, text='自动重登（无人值守）', style='Recovery.TCheckbutton', variable=self.native_enabled,
                        command=self.toggle_native_recovery).pack(side='left')
        self.native_status = self.label(recovery_bar, fg=MUTED, size=9)
        self.native_status.pack(side='left', padx=12)

        footer = tk.Frame(self, bg=BG)
        self.monitor_footer = footer
        footer.pack(side='bottom', fill='x', pady=(12, 0))
        self.timestamps = self.label(footer, fg=MUTED, anchor='w', size=9)
        self.timestamps.pack(fill='x', pady=(0, 8))
        self.button(footer, '立即检查', self.check, True).pack(side='left')
        self.pause_button = self.button(footer, '暂停库存监控', self.toggle)
        self.pause_button.pack(side='left', padx=8)
        self.button(footer, '收起到托盘', self.on_hide).pack(side='right')
        footer_notice = self.label(footer, textvariable=self.notice, fg='#BA6415', anchor='w', justify='left')
        footer_notice.pack(fill='x', side='top', before=self.timestamps, pady=(0, 6))
        wrap_label(footer_notice)
        def show_footer_notice(*_):
            if self.notice.get():
                footer_notice.pack(fill='x', side='top', before=self.timestamps, pady=(0, 6))
            else:
                footer_notice.pack_forget()
        self.notice.trace_add('write', show_footer_notice)

        body = tk.PanedWindow(self, bg=BG, sashwidth=14, bd=0, orient='horizontal')
        self.body = body
        body.pack(fill='both', expand=True)
        left_shell, right_shell = RoundedCard(body), RoundedCard(body)
        left, right = left_shell.content, right_shell.content
        self.monitor_page, self.product_page = left_shell, right_shell
        body.add(left_shell, stretch='always', minsize=310, width=400)
        body.add(right_shell, stretch='always', minsize=430, width=620)
        bar = tk.Frame(left, bg='white')
        bar.pack(fill='x', pady=(0, 10))
        self.label(bar, '重点产品', size=14).pack(side='left', padx=(0, 12))
        self.button(bar, '+ 添加产品', self.choose_catalog, True).pack(side='right')
        self.button(bar, '表格导入', self.open_import).pack(side='right', padx=(0, 8))
        actions = tk.Frame(left, bg='white')
        actions.pack(fill='x', pady=(0, 8))
        self.label(actions, '筛选产品', fg=MUTED, size=9).pack(side='left', padx=(0, 10))
        ttk.Entry(actions, textvariable=self.search, font=('Microsoft YaHei UI', 10), width=14).pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.empty = self.label(left, '添加重点产品后，将在 ERP 在线时检查公司大库。', fg=MUTED, anchor='w')
        self.empty.pack(fill='x', pady=(0, 6))
        wrap_label(self.empty)
        self.remove_button = self.button(actions, '删除关注', self.remove_selected)
        self.remove_button.configure(fg='#B42332', pady=4)
        self.remove_button.pack(side='right', padx=(8, 0))
        edit_button = self.button(actions, '编辑配置', self.edit_selected)
        edit_button.configure(pady=4)
        edit_button.pack(side='right')
        table = tk.Frame(left, bg='white')
        table.pack(fill='both', expand=True)
        columns = ('name', 'able', 'purchase', 'threshold', 'risk')
        self.tree = ttk.Treeview(table, columns=columns, show='headings', style='Inventory.Treeview', selectmode='browse')
        for key, title, width in zip(columns, ('产品名称 / ERP 编号', '预警数量', '待入', '预警值', '状态'), (220, 70, 70, 75, 100)):
            self.tree.heading(key, text=title, anchor='w' if key == 'name' else 'center')
            self.tree.column(key, width=width, minwidth=45, anchor='w' if key in ('name', 'risk') else 'e', stretch=key == 'name')
        scroll = ScrollBar(table, command=self.tree.yview)
        horizontal = ScrollBar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        for tag, shade in (('negative_stock', '#FFF0F2'), ('oversold', '#FFF0F2'), ('low', '#FFF8EC'), ('normal', 'white'), ('unknown', '#F7F8FA'), ('disabled', '#F7F8FA')):
            self.tree.tag_configure(tag, foreground=MUTED if tag in ('unknown', 'disabled') else INK, background=shade)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.tree.bind('<Double-1>', self.edit_selected)
        event_area = tk.Frame(left, bg='white')
        event_area.pack(side='bottom', fill='x', before=table)
        self.event_area = event_area
        self._events_manual = None
        event_toggle = self.button(event_area, '最近动态  ▾', self.toggle_events)
        event_toggle.configure(bg='white', padx=0, pady=5, highlightbackground='white', anchor='w')
        event_toggle.pack(fill='x', pady=(6, 0))
        self.event_toggle = event_toggle
        event_content = tk.Frame(event_area, bg='white')
        event_content.pack(fill='x')
        self.event_content = event_content
        self.inbound_note = self.label(event_content, fg=MUTED, anchor='w', size=9)
        self.inbound_note.pack(fill='x', pady=(0, 5))
        self.events = tk.Text(event_content, height=3, width=1, bg='#F7F8FB', fg=MUTED, bd=0, padx=10, pady=8, wrap='word', state='disabled', font=('Microsoft YaHei UI', 9))
        event_scroll = ScrollBar(event_content, command=self.events.yview)
        self.events.configure(yscrollcommand=event_scroll.set)
        event_scroll.pack(side='right', fill='y')
        self.events.pack(fill='x', expand=True)
        self.bind('<Configure>', lambda e: self.layout_events() if e.widget == self else None, add='+')

        self.add_tabs = ttk.Notebook(right)
        self.add_tabs.pack(fill='both', expand=True)
        self.search_page = tk.Frame(self.add_tabs, bg=BG)
        self.config_page = tk.Frame(self.add_tabs, bg='white')
        self.add_tabs.add(self.search_page, text='  搜索产品  ')
        self.add_tabs.add(self.config_page, text='  产品配置  ')
        self.build_catalog(self.search_page)
        right = self.config_page
        navigation = tk.Frame(right, bg='white', padx=14, pady=8)
        navigation.pack(side='top', fill='x')
        self.back_to_search_button = self.button(navigation, '← 返回搜索产品', self.choose_catalog)
        self.back_to_search_button.configure(pady=4)
        self.back_to_search_button.pack(side='left')
        buttons = tk.Frame(right, bg='white', padx=14, pady=12)
        buttons.pack(side='bottom', fill='x')
        self.save_button = self.button(buttons, '保存配置', self.save, True)
        self.save_button.pack(side='left')
        self.button(buttons, '取消并返回', self.cancel_and_return).pack(side='right')
        canvas = tk.Canvas(right, bg='white', highlightthickness=0, width=310)
        scroll = ScrollBar(right, command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        bind_wheel(canvas)
        self.editor_canvas = canvas
        form = tk.Frame(canvas, bg='white', padx=12, pady=8)
        window = canvas.create_window((0, 0), window=form, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        form.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        self.editor_title = self.label(form, '产品配置', size=12, anchor='w')
        self.editor_title.pack(fill='x', pady=(0, 3))
        for key, title in (('name', '商品名称'), ('goods_id', 'ERP 编号'), ('sku', 'SKU 编码'), ('category', '分类 / 档位'), ('threshold', '预警数量'), ('keywords', '关联关键词')):
            field_row = tk.Frame(form, bg='white')
            field_row.pack(fill='x', pady=5)
            self.label(field_row, title, anchor='w', size=9, width=9).pack(side='left')
            if key == 'category':
                widget = ttk.Combobox(field_row, textvariable=self.vars[key], values=CATEGORIES, state='readonly', width=12)
                widget.bind('<<ComboboxSelected>>', self.category_changed)
            elif key == 'threshold':
                threshold_row = tk.Frame(field_row, bg='white')
                threshold_row.pack(fill='x', expand=True)
                widget = ttk.Spinbox(threshold_row, textvariable=self.vars[key], from_=0, to=999999999, increment=1, width=10)
                widget.pack(side='left', fill='x', expand=True)
                default_button = self.button(threshold_row, '用默认值', self.apply_default)
                default_button.configure(padx=7, pady=0, font=('Microsoft YaHei UI', 9))
                default_button.pack(side='right', padx=(6, 0))
            else:
                widget = ttk.Entry(field_row, textvariable=self.vars[key], width=12)
            if key != 'threshold':
                widget.pack(fill='x', expand=True)
            if key == 'sku':
                self.sku_entry = widget
            if key == 'goods_id':
                widget.configure(state='readonly')
            if key == 'name':
                self.name_entry = widget
        basis_row = tk.Frame(form, bg='white')
        basis_row.pack(fill='x', pady=5)
        self.label(basis_row, '预警依据', anchor='w', size=9, width=9).pack(side='left')
        self.basis_choice = tk.StringVar(self)
        self.basis_combo = ttk.Combobox(basis_row, textvariable=self.basis_choice,
                                       values=('公司大库可销数', '指定分库库存数'), state='readonly')
        self.basis_combo.pack(fill='x', expand=True)
        self.basis_combo.bind('<<ComboboxSelected>>', self.change_basis)
        warehouse_row = tk.Frame(form, bg='white')
        warehouse_row.pack(fill='x', pady=5)
        self.label(warehouse_row, '指定分库', anchor='w', size=9, width=9).pack(side='left')
        self.warehouse_choice = tk.StringVar(self)
        self.warehouse_combo = ttk.Combobox(warehouse_row, textvariable=self.warehouse_choice, state='disabled')
        self.warehouse_combo.pack(fill='x', expand=True)
        self.warehouse_combo.bind('<<ComboboxSelected>>', self.choose_warehouse)
        self.button(form, '刷新分库', self.request_warehouses).pack(anchor='e')
        self.warehouse_state = tk.StringVar(self)
        hint = self.label(form, textvariable=self.warehouse_state, fg=MUTED, anchor='w', size=9, justify='left')
        hint.pack(fill='x', pady=(3, 0))
        wrap_label(hint)
        self.default_hint = self.label(form, '', fg=MUTED, anchor='w', size=9)
        self.default_hint.pack(fill='x', pady=(3, 0))
        ttk.Checkbutton(form, text='启用此产品监控', variable=self.enabled).pack(fill='x', pady=(10, 4))
        alerts_grid = tk.Frame(form, bg='white')
        alerts_grid.pack(fill='x')
        self.alert_widgets = []
        for index, (key, title) in enumerate(ALERTS.items()):
            check = ttk.Checkbutton(alerts_grid, text=title, variable=self.alerts[key])
            check.grid(row=index // 2, column=index % 2, sticky='w', padx=(0, 16))
            self.alert_widgets.append(check)
        quick_actions = tk.Frame(form, bg='white')
        quick_actions.pack(fill='x', pady=(5, 0))
        catalog_button = self.button(quick_actions, '搜索 ERP 产品', self.choose_catalog)
        catalog_button.configure(padx=6, pady=2, font=('Microsoft YaHei UI', 9))
        catalog_button.pack(side='left')
        self.setup_button = self.button(quick_actions, '采集脚本设置', self.open_setup)
        self.setup_button.configure(padx=6, pady=2, font=('Microsoft YaHei UI', 9))
        self.setup_button.pack(side='right')

    def change_basis(self, *_):
        self.vars['stock_basis'].set('warehouse_stock' if self.basis_choice.get() == '指定分库库存数' else 'company_able')
        self.refresh_warehouses()

    def request_warehouses(self):
        try:
            self.service.request_warehouses(self.vars['sku'].get().strip(), self.vars['goods_id'].get().strip())
            self.refresh_warehouses()
        except ValueError as error:
            self.warehouse_state.set(str(error))

    def choose_warehouse(self, *_):
        depot = self._warehouse_choices.get(self.warehouse_choice.get())
        if depot:
            self.vars['warehouse_id'].set(str(depot['id']))
            self.vars['warehouse_name'].set(depot['name'])

    def refresh_warehouses(self):
        selected_id = self.vars['warehouse_id'].get()
        enabled = self.vars['stock_basis'].get() == 'warehouse_stock'
        self.basis_choice.set('指定分库库存数' if enabled else '公司大库可销数')
        goods_id = self.vars['goods_id'].get().strip()
        result = self.service.warehouse_options(goods_id) if goods_id else {'status': 'missing'}
        ready = result.get('status') == 'ready'
        depots = result.get('depots', []) if ready else []
        self._warehouse_choices = {}
        for depot in depots:
            label = f"{depot['name']} · 库存 {depot.get('stock') if depot.get('stock') is not None else '未知'}"
            if sum(d['name'] == depot['name'] for d in depots) > 1:
                label += f" · 同名库编号 {depot['id']}"
            self._warehouse_choices[label] = depot
        self.warehouse_combo.configure(values=tuple(self._warehouse_choices), state='readonly' if enabled and ready else 'disabled')
        saved_label = next((label for label, d in self._warehouse_choices.items() if str(d['id']) == selected_id), '')
        if not saved_label and selected_id:
            saved_label = self.vars['warehouse_name'].get() + ' · 当前选择（待核验）'
        self.warehouse_choice.set(saved_label)
        states = {'missing': '点击“刷新分库”读取当前产品分库。', 'pending': '正在读取分库，请保持 ERP 连接。',
                  'ready': '请选择一个分库；未选择的库不计入预警。', 'error': '读取失败：' + str(result.get('error') or '请重试')}
        self.warehouse_state.set('来源：库存查询 → 总库存 → 分库数查询。\n' + states.get(result.get('status'), states['missing']))

    def category_changed(self, *_):
        value = ProductDraft(category=self.vars['category'].get()).suggested_threshold()
        self.default_hint.config(text=f'分类建议：{value}；当前输入优先保存' if value is not None else '此分类需要手动填写预警数量')
        if not self.vars['threshold'].get():
            self.vars['threshold'].set('' if value is None else str(value))

    def layout_events(self):
        expanded = self._events_manual if self._events_manual is not None else self.winfo_toplevel().winfo_height() >= 760
        if expanded:
            self.event_content.pack(fill='x')
        else:
            self.event_content.pack_forget()
        self.event_toggle.configure(text='最近动态  ▾' if expanded else '最近动态  ▸  点击展开')

    def toggle_events(self):
        self._events_manual = not bool(self.event_content.winfo_manager())
        self.layout_events()

    def apply_default(self):
        value = ProductDraft(category=self.vars['category'].get()).suggested_threshold()
        self.vars['threshold'].set('' if value is None else str(value))
        self.category_changed()

    def load_product(self, product):
        if product.get('sku') and not product.get('goods_id'):
            match = next((r for r in self.service.catalog(product['sku'], limit=None) if r['sku'] == product['sku']), None)
            if match:
                product = dict(product, goods_id=match.get('goods_id', ''))
        draft = ProductDraft.from_product(product)
        for key, var in self.vars.items():
            var.set(getattr(draft, key))
        self.enabled.set(draft.enabled)
        for key, var in self.alerts.items():
            var.set(draft.alerts[key])
        self.sku_entry.configure(state='readonly' if self.selected_sku else 'normal')
        self.category_changed()
        self.refresh_warehouses()

    def new_product(self, reveal=True):
        self.selected_sku = None
        self.tree.selection_remove(self.tree.selection())
        self.load_product({})
        self.editor_title.configure(text='添加重点产品', fg=BLUE)
        self.save_button.configure(text='添加并保存')
        self.notice.set('已进入新增模式：请在右侧填写商品名称和真实编码，然后点击“添加并保存”。')
        if reveal:
            if getattr(self, 'page_navigation', False):
                self.show_module('config')
            self.add_tabs.select(self.config_page)
            self.update_idletasks()
            if not getattr(self, 'page_navigation', False):
                self.body.sash_place(0, max(310, int(self.body.winfo_width() * .39)), 0)
            self.editor_canvas.yview_moveto(0)
            self.name_entry.focus_set()

    def select(self, *_):
        selected = self.tree.selection()
        if selected and selected[0] in self.rows:
            self.selected_sku = self.rows[selected[0]]['sku']
            self.load_product(self.rows[selected[0]])
            if not getattr(self, 'page_navigation', False):
                self.add_tabs.select(self.config_page)
            self.editor_title.configure(text='编辑产品配置', fg=INK)
            self.save_button.configure(text='保存配置')
            self.notice.set('修改后点击保存生效；取消会还原当前产品。')

    def remove_selected(self):
        from inventory_remove_ui import RemoveWindow
        window = getattr(self, 'remove_window', None)
        if window is not None and window.winfo_exists():
            window.lift()
            return window
        def removed(results):
            if self.selected_sku in {r['sku'] for r in results}:
                self.new_product(reveal=False)
            self.refresh()
            self.notice.set(f'已删除 {len(results)} 个产品关注。')
        self.remove_window = RemoveWindow(self, self.service, on_removed=removed)
        return self.remove_window

    def cancel(self):
        product = next((p for p in self.service.products() if p['sku'] == self.selected_sku), None)
        if product:
            self.load_product(product)
            self.notice.set('已还原已保存的配置。')
        else:
            self.new_product()

    def cancel_and_return(self):
        self.cancel()
        self.choose_catalog()
        self.notice.set('已取消未保存的修改，返回搜索产品。')

    def open_import(self):
        from inventory_import_ui import ImportWindow
        window = getattr(self, 'import_window', None)
        if window is not None and window.winfo_exists():
            window.lift()
            return window
        self.import_window = ImportWindow(self, self.service, on_saved=self.refresh)
        return self.import_window

    def save(self):
        try:
            draft = ProductDraft(**{key: var.get() for key, var in self.vars.items()}, enabled=self.enabled.get(), alerts={key: var.get() for key, var in self.alerts.items()})
            if not self.selected_sku and any(p['sku'] == draft.sku.strip() for p in self.service.products()):
                raise ValueError('此编码已在关注清单中，请选择已有产品进行编辑。')
            self.service.save_product(draft.payload())
            self.selected_sku = draft.sku.strip()
            self.editor_title.configure(text='编辑产品配置', fg=INK)
            self.save_button.configure(text='保存配置')
            self.sku_entry.configure(state='readonly')
            self.notice.set('配置已保存。ERP 在线时自动检查，手动预警值已保留。')
            self.refresh()
            return True
        except ValueError as exc:
            self.notice.set(str(exc))
            return False

    def toggle(self):
        self.service.set_enabled(not self.service.status()['enabled'])
        self.refresh()

    def check(self):
        self.service.request_check()
        self.notice.set('已请求立即检查；等待已登录的 ERP 采集页面响应。')
        self.refresh()

    def build_catalog(self, parent):
        win = parent
        area = tk.Frame(win, bg='white', padx=12, pady=12)
        area.pack(fill='both', expand=True)
        self.label(area, '添加重点产品', size=15).pack(anchor='w', pady=(0, 6))
        modes = tk.Frame(area, bg='white')
        modes.pack(fill='x')
        self.catalog_mode = tk.StringVar(self, 'name')
        for text, value in [('商品名称', 'name'), ('ERP 编号（精确）', 'goods_id')]:
            ttk.Radiobutton(modes, text=text, value=value, variable=self.catalog_mode).pack(side='left', padx=(0, 16))
        query = tk.StringVar(win)
        entry = ttk.Entry(area, textvariable=query, font=('Microsoft YaHei UI', 11))
        self.catalog_entry = entry
        entry.pack(fill='x', pady=(6, 8))
        summary = self.label(area, '', fg=MUTED, anchor='w')
        summary.pack(fill='x', pady=(0, 8))
        wrap_label(summary)
        table = tk.Frame(area, bg='white')
        table.pack(fill='both', expand=True)
        columns = ('goods_id', 'name', 'able', 'purchase', 'stock')
        tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='browse', height=4)
        for key, title, width in zip(columns, ('ERP 编号', '商品名称', '可销', '待入', '实际'), (85, 220, 55, 55, 55)):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=45, stretch=key == 'name')
        scrollbar = ScrollBar(table, command=tree.yview)
        horizontal = ScrollBar(table, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        candidates = {}
        def refresh_results(*_):
            matches = self.service.catalog(query.get().strip(), limit=None, mode=self.catalog_mode.get())
            total = len(self.service.catalog(limit=None))
            tree.delete(*tree.get_children())
            candidates.clear()
            for product in matches[:200]:
                sku = product['sku']
                candidates[sku] = product
                tree.insert('', 'end', iid=sku, values=(product.get('goods_id', '—'), product['name'],
                    *[product.get(key) if product.get(key) is not None else '—' for key in ('able', 'purchase', 'stock')]))
            if not total:
                summary.config(text='尚无完整采集目录，请先连接 ERP 并完成采集。')
            else:
                checked = self.service.status().get('checked_at')
                stamp = time.strftime('%m-%d %H:%M', time.localtime(checked)) if checked else '未知'
                summary.config(text=f'已采集 {total:,} 件 · 匹配 {len(matches):,} 件 · 显示前 200 件 · 库存快照 {stamp}')
        def choose(*_):
            selected = tree.selection()
            if not selected:
                summary.config(text='请先在结果列表中选择一个产品。')
                return
            product = candidates[selected[0]]
            saved = next((p for p in self.service.products() if p['sku'] == product['sku']), None)
            if saved:
                self.selected_sku = saved['sku']
                saved = dict(saved, goods_id=saved.get('goods_id') or product.get('goods_id', ''))
                self.load_product(saved)
                self.editor_title.configure(text='编辑产品配置', fg=INK)
                self.save_button.configure(text='保存配置')
                self.notice.set('此产品已关注，已打开原配置，可直接修改。')
            else:
                self.new_product()
                self.vars['name'].set(product['name'])
                self.vars['sku'].set(product['sku'])
                self.vars['goods_id'].set(str(product.get('goods_id', '')))
                self.notice.set('已选择 ERP 产品：请核对分类和预警数量，点击“添加并保存”。')
            if getattr(self, 'page_navigation', False):
                self.show_module('config')
            self.add_tabs.select(self.config_page)
            self.editor_canvas.yview_moveto(0)
        self.button(area, '选择并配置', choose, True).pack(side='bottom', anchor='e', pady=(10, 0), before=table)
        query.trace_add('write', refresh_results)
        self.catalog_mode.trace_add('write', refresh_results)
        self.refresh_catalog = refresh_results
        tree.bind('<Double-1>', choose)
        tree.bind('<Return>', choose)
        # Expose controls for the isolated real-window acceptance check.
        self.catalog_query, self.catalog_results = query, tree
        self.catalog_choose = choose
        refresh_results()
    def choose_catalog(self):
        if getattr(self, 'page_navigation', False):
            self.show_module('search')
        self.add_tabs.select(self.search_page)
        self.refresh_catalog()
        self.catalog_entry.focus_set()
        self.catalog_entry.selection_range(0, 'end')
        self.notice.set('选择搜索方式，输入 ERP 编号或商品名称，再双击搜索结果。')

    def edit_selected(self, *_):
        if not self.tree.selection():
            self.notice.set('请先选中要编辑的产品。')
            return
        self.select()
        if getattr(self, 'page_navigation', False):
            self.show_module('config')

    def show_module(self, module):
        if getattr(self, 'active_module', None) != module:
            self.notice.set('')
        self.page_navigation = True
        self.add_tabs.configure(style='Page.TNotebook')
        self.active_module = module
        if not hasattr(self, 'page_notice'):
            self.page_notice = self.label(self, textvariable=self.notice, fg='#BA6415', anchor='w', wraplength=1000)
        for pane in self.body.panes():
            self.body.forget(pane)
        if module == 'monitor':
            self.page_notice.pack_forget()
            self.body.add(self.monitor_page, stretch='always', minsize=310)
            self.monitor_footer.pack(side='bottom', fill='x', pady=(12, 0), before=self.body)
        else:
            self.monitor_footer.pack_forget()
            self.page_notice.pack(side='bottom', fill='x', pady=(8, 0), before=self.body)
            if module == 'dingtalk':
                if not hasattr(self, 'dingtalk_page'):
                    from dingtalk_panel import DingTalkPanel
                    self.dingtalk_page = RoundedCard(self.body)
                    DingTalkPanel(self.dingtalk_page.content, self.dingtalk_controller).pack(fill='both', expand=True)
                target = self.dingtalk_page
            elif module == 'daily':
                if not hasattr(self, 'daily_page'):
                    from inventory_daily import build_page
                    self.daily_page = build_page(self.body, self.service)
                target = self.daily_page
            elif module == 'settings':
                if not hasattr(self, 'settings_page'):
                    self.settings_page = tk.Frame(self.body, bg=BG, padx=0, pady=0)
                    settings_canvas = tk.Canvas(self.settings_page, bg=BG, highlightthickness=0)
                    settings_scroll = ScrollBar(self.settings_page, command=settings_canvas.yview, bg=BG)
                    settings_scroll.pack(side='right', fill='y')
                    settings_canvas.pack(fill='both', expand=True)
                    settings_canvas.configure(yscrollcommand=settings_scroll.set)
                    bind_wheel(settings_canvas)
                    settings_content = tk.Frame(settings_canvas, bg=BG, padx=2)
                    settings_window = settings_canvas.create_window((0, 0), window=settings_content, anchor='nw')
                    settings_canvas.bind('<Configure>', lambda e: settings_canvas.itemconfigure(settings_window, width=e.width))
                    settings_content.bind('<Configure>', lambda e: settings_canvas.configure(scrollregion=settings_canvas.bbox('all')))
                    from inventory_cache_ui import build_section as build_cache_section
                    self.cache_panel = build_cache_section(settings_content, self.service)
                    interval_card = section(settings_content, '监控周期', '按设定周期检查公司大库；也可随时点击“立即检查”。')
                    interval_row = tk.Frame(interval_card, bg='white')
                    interval_row.pack(fill='x', pady=(0, 14))
                    self.interval_minutes = tk.StringVar(self, str(self.service.status()['interval_seconds'] // 60))
                    ttk.Spinbox(interval_row, textvariable=self.interval_minutes, from_=1, to=10080,
                                width=7, font=('Microsoft YaHei UI', 12)).pack(side='left')
                    self.label(interval_row, '分钟', size=12).pack(side='left', padx=(10, 18))
                    hours_hint = self.label(interval_row, '', fg=BLUE, size=13)
                    hours_hint.pack(side='left')
                    def update_hours(*_):
                        value = self.interval_minutes.get().strip()
                        if value.isascii() and value.isdecimal() and 1 <= int(value) <= 10080:
                            hours, minutes = divmod(int(value), 60)
                            text = f'{hours} 小时' if minutes == 0 else f'{hours} 小时 {minutes} 分钟'
                            hours_hint.config(text='=  ' + text)
                        else:
                            hours_hint.config(text='请输入有效分钟数')
                    self.interval_minutes.trace_add('write', update_hours)
                    update_hours()

                    def save_interval():
                        try:
                            seconds = self.service.set_interval(self.interval_minutes.get())
                            self.notice.set(f'监控周期已改为 {seconds // 60} 分钟，下次检查时间已更新。')
                            self.refresh()
                        except ValueError as error:
                            self.notice.set(str(error))
                    self.button(interval_row, '保存周期', save_interval, True).pack(side='right', padx=(18, 0))
                    erp_card = section(settings_content, 'ERP 采集连接', '保持 ERP 分库库存页面打开，采集完成后库存会自动更新。')
                    instructions = '1. 在 ERP 浏览器启用 inventory-erp.user.js。\n2. 登录 ERP，打开分库库存，选择公司大库。\n3. 点击右下角“库存采集：点击连接”，核对账号。\n4. 返回库存监控，点击“立即检查”。'
                    instructions_label = self.label(erp_card, instructions, justify='left', anchor='w')
                    instructions_label.pack(fill='x')
                    wrap_label(instructions_label)
                    self.button(erp_card, '查看采集说明', self.open_setup).pack(anchor='w', pady=(14, 0))
                    from notification_robot_ui import RobotPanel
                    from notification_robots import store_for_service
                    robot_card = section(settings_content, '通知机器人')
                    self.robot_panel = RobotPanel(robot_card, store=store_for_service(self.service))
                    self.robot_panel.pack(fill='x')
                else:
                    self.robot_panel.refresh()
                target = self.settings_page
            else:
                target = self.product_page
                chosen, other = (self.search_page, self.config_page) if module == 'search' else (self.config_page, self.search_page)
                self.add_tabs.add(chosen)
                self.add_tabs.hide(other)
                self.add_tabs.select(chosen)
            self.body.add(target, stretch='always', minsize=430)
        if hasattr(self, 'on_module_changed'):
            self.on_module_changed(module)

    def open_setup(self):
        if self.setup_path.is_file():
            os.startfile(str(self.setup_path))
        else:
            self.notice.set('安装说明尚未随程序提供，请联系维护者补充 inventory-setup.md。')

    def toggle_native_recovery(self):
        with self.service.db() as db:
            self.service.put(db, 'erp_native_enabled', self.native_enabled.get())
            if self.native_enabled.get():
                self.service.put(db, 'erp_native_state', {})
        self.notice.set('自动重登已开启，请让浏览器停留在 ERP 页面完成首次绑定。' if self.native_enabled.get() else '自动重登已关闭，库存监控继续运行。')
        self.refresh()

    def refresh(self):
        with self.service.db() as db:
            native = self.service.get(db, 'erp_native_state', {})
        labels = {'wrong_tab':'请切到 ERP 页面完成绑定','browser_unavailable':'浏览器连接不可用，请检查 OpenCLI 扩展',
                  'connected':'ERP 已绑定','waiting_fill':'等待浏览器填充','waiting_result':'等待登录结果',
                  'clicking':'正在点击登录','manual_required':'需要人工验证','exhausted':'重试失败，等待人工检查'}
        self.native_status.configure(text=labels.get(native.get('status'),'等待 ERP 连接') if self.native_enabled.get() else '已关闭，不操作登录页面')
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        try:
            status = self.service.status()
            self.refresh_warehouses()
            if getattr(self, '_catalog_checked', None) != status.get('checked_at'):
                self._catalog_checked = status.get('checked_at')
                self.refresh_catalog()
            self.message_state.config(text=f"每 {status.get('interval_seconds', 7200) / 3600:g} 小时检查 · 按产品预警依据")
            state = status.get('connection', 'waiting')
            if hasattr(self, 'sidebar_erp_status'):
                connection_label, shade = {
                    'online': ('ERP 已连接', '#19B790'),
                    'waiting': ('等待 ERP 连接', '#D99A42'),
                    'stale': ('ERP 连接已断开', '#D99A42'),
                    'error': ('ERP 连接异常', '#EF6B73'),
                }.get(state, ('ERP 状态未知', '#D99A42'))
                self.sidebar_erp_status.config(text='●  ' + connection_label, fg=shade)
            text = {'waiting': '等待 ERP', 'online': 'ERP 在线', 'stale': '数据过期', 'error': 'ERP 连接异常'}.get(state, '等待 ERP')
            self.connection.config(text='● ' + text + ('' if status['enabled'] else ' · 监控已暂停'), fg='#087B54' if state == 'online' else '#BA6415')
            self.delivery_state.config(text=delivery_summary(status))
            detail = str(status.get('connection_error') or '')
            if state != 'online' and status.get('checked_at'):
                detail = (detail + ' · ' if detail else '') + '下列数量保留自上次成功检查，当前库存尚未确认。'
            self.connection_detail.config(text=detail)
            if detail:
                self.connection_detail.pack(fill='x', pady=(0, 6), after=self.delivery_state)
            else:
                self.connection_detail.pack_forget()
            self.inbound_note.config(text='' if status.get('inbound_verified') else '入库单据尚未接入，等待 ERP 采集')
            self.pause_button.config(text='暂停库存监控' if status['enabled'] else '启用库存监控')
            fmt = lambda value: time.strftime('%m-%d %H:%M:%S', time.localtime(value)) if value else '—'
            self.timestamps.config(text=f"最近成功检查：{fmt(status.get('checked_at'))}    下次检查：{fmt(status.get('next_check'))}    每 {status.get('interval_seconds', 7200) // 60} 分钟 · 按产品预警依据")
            query = self.search.get().strip().casefold()
            products = [p for p in status.get('products', []) if query in (p['name'] + ' ' + p['sku'] + ' ' + str(p.get('goods_id', ''))).casefold()]
            self.empty.config(text=f"已关注 {len(status.get('products', []))} 个产品 · 未取得库存时显示待检查" if status.get('products') else '尚未添加产品，点击“添加产品”开始。')
            self.rows = {p['sku']: p for p in products}
            table_rows = []
            for iid, p in self.rows.items():
                label, risk = product_risk_label(p, state)
                qty = lambda key: '—' if p.get(key) is None else str(p[key])
                marker = {'low': '● ', 'oversold': '● ', 'negative_stock': '● ', 'normal': '○ ', 'unknown': '— ', 'disabled': '— '}.get(risk, '')
                values = (f"{p['name']}\n{p.get('goods_id') or '编号待采集'} · {p.get('warehouse_name', '') + ' · 库存' if p.get('stock_basis') == 'warehouse_stock' else '公司大库 · 可销'}", qty('monitor_qty' if p.get('stock_basis') == 'warehouse_stock' else 'able'), qty('warehouse_purchase' if p.get('stock_basis') == 'warehouse_stock' else 'purchase'), p['threshold'], marker + label)
                table_rows.append((iid, values, (risk,)))
            sync_tree(self.tree, table_rows)
            events = status.get('events', [])[:6]
            event_names = {'out_of_stock': '库存归零', 'stock_increase': '库存增加', 'inbound': '入库', 'pending_inbound': '待入库', 'low': '低库存', 'oversold': '超售', 'negative_stock': '负库存', 'recovery': '风险恢复'}
            delivery_names = {'pending': '待发送', 'sent': '已发送', 'unknown': '待核实', 'failed': '失败待重试', 'sending': '发送中', 'cancelled': '已取消', 'partial': '部分送达', 'skipped': '已跳过'}
            lines = [f"{fmt(e.get('created_at', e.get('observed_at', 0)))}  {e.get('summary') or e.get('message') or event_names.get(e.get('kind'), '库存变化')}  {e.get('name', e.get('sku', ''))} · {delivery_names.get(e.get('delivery_status'), '待发送')}" for e in events]
            lines = [line + recipient_summary(event) for line, event in zip(lines, events)]
            event_text = '\n'.join(lines) or '暂无库存动态；尚未检查不代表库存为 0。'
            if event_text != getattr(self, '_event_text', None):
                self.events.configure(state='normal')
                self.events.delete('1.0', 'end')
                self.events.insert('end', event_text)
                self.events.configure(state='disabled')
                self._event_text = event_text
        except Exception:
            self.connection.config(text='● 库存服务异常', fg='#B42332')
            if hasattr(self, 'sidebar_erp_status'):
                self.sidebar_erp_status.config(text='●  ERP 状态读取失败', fg='#EF6B73')
            self.notice.set('读取库存状态失败，请检查本地数据文件；原配置不会被重置。')
        self._after = self.after(2000, self.refresh)

    def destroy(self):
        if self._after:
            self.after_cancel(self._after)
        super().destroy()
