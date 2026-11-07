"""抖店库存工作页：店铺与状态在上，库存和变化记录占主要阅读空间。"""
from datetime import datetime
from pathlib import Path
import os
import re
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from douyin_scope import machine_rows, product_url
from inventory_theme import BG, INK, MUTED, BLUE, RoundedCard, button, sync_tree, wrap_label
from inventory_insight_ui import table


class DouyinPanel(tk.Frame):
    def __init__(self, parent, runtime):
        super().__init__(parent, bg=BG)
        self.runtime, self.after_id = runtime, None
        self.ident = None
        self.shop_rows, self.cached_rows = [], []
        self.cache_key = None
        self.notice = tk.StringVar(self)
        self.search = tk.StringVar(self)
        self.filter = tk.StringVar(self, '全部库存')
        self.interval = tk.StringVar(self, '30')
        self.export_path = tk.StringVar(self)
        self.patrol_widgets = {}
        self.batch_notice = tk.StringVar(self)

        # 常用操作常驻一行，低频连接与间隔设置折叠，给 SKU 表保留阅读空间。
        card = RoundedCard(self)
        card.pack(fill='x', pady=(0, 8))
        content = card.content
        content.configure(pady=0)
        line = tk.Frame(content, bg='white'); line.pack(fill='x')
        tk.Label(line, text='查看店铺', bg='white', fg=INK, font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=(0, 10))
        self.shop = ttk.Combobox(line, state='readonly', width=22)
        self.shop.pack(side='left', fill='x', expand=True, padx=(0, 12))
        self.shop.bind('<<ComboboxSelected>>', self.select_store)
        self.export_button = button(line, '立即检查', self.export, primary=True, pady=7)
        self.export_button.pack(side='left', padx=(0, 8))
        self.toggle_button = button(line, '启用定时监控', self.toggle, pady=7)
        self.toggle_button.pack(side='left', padx=(0, 8))
        self.settings_button = button(line, '监控设置', self.toggle_settings, padx=12, pady=7)
        self.settings_button.pack(side='left')
        meta = tk.Frame(content, bg='white'); meta.pack(fill='x', pady=(8, 0))
        self.summary = tk.Label(meta, bg='white', fg=MUTED, anchor='w', font=('Microsoft YaHei UI', 9))
        self.summary.pack(side='left', fill='x', expand=True)
        self.notification = tk.Label(meta, text='群提醒：关闭', bg='white', fg=MUTED, font=('Microsoft YaHei UI', 9))
        self.notification.pack(side='right')
        # 勾选控制实际巡检范围；查看下拉框只切换结果，不影响其他店铺任务。
        patrol_area = tk.Frame(content, bg='white')
        patrol_area.pack(fill='x', pady=(7, 0))
        tk.Label(patrol_area, text='参与巡检', bg='white', fg=MUTED, anchor='nw',
                 font=('Microsoft YaHei UI', 9)).pack(side='left', anchor='n', padx=(0, 8), pady=6)
        self.patrol_box = tk.Frame(patrol_area, bg='white')
        self.patrol_box.pack(side='left', fill='x', expand=True)
        self.patrol_box.bind('<Configure>', self.layout_patrol)
        self.settings_box = tk.Frame(content, bg='white')
        settings_line = tk.Frame(self.settings_box, bg='white'); settings_line.pack(fill='x', pady=(12, 8))
        tk.Label(settings_line, text='巡检间隔', bg='white', fg=MUTED).pack(side='left', padx=(0, 8))
        ttk.Spinbox(settings_line, from_=5, to=1440, width=5, textvariable=self.interval).pack(side='left')
        tk.Label(settings_line, text='分钟', bg='white', fg=MUTED).pack(side='left', padx=8)
        button(settings_line, '保存间隔', self.save_interval, padx=10, pady=5).pack(side='left', padx=(0, 12))
        button(settings_line, '店铺管理', self.shops_dialog, padx=10, pady=5).pack(side='left', padx=(0, 8))
        button(settings_line, '连接店铺', lambda: self.action('login'), padx=10, pady=5).pack(side='left', padx=(0, 8))
        button(settings_line, '登录设置', self.login_settings, padx=10, pady=5).pack(side='left', padx=(0, 8))
        button(settings_line, '主机维护表', self.maintenance_settings, padx=10, pady=5).pack(side='left')
        from douyin_low_stock_ui import open_rules
        button(self.settings_box, '低库存预警规则', lambda: open_rules(self), padx=10, pady=5).pack(anchor='w', pady=(0, 6))
        self.source_label = tk.Label(self.settings_box, bg='white', fg=MUTED, anchor='w', font=('Microsoft YaHei UI', 9))
        self.source_label.pack(fill='x', pady=(6, 0))
        self.cleanup_label = tk.Label(self.settings_box, bg='white', fg=MUTED, anchor='w',
            text='历史导出每 3 天清理一次：移入回收站，保留最新有效表。')
        self.cleanup_label.pack(fill='x', pady=(0, 6))
        button(settings_line, '监控说明', self.help, padx=10, pady=5).pack(side='right')

        # 单一状态栏只显示当前店铺的结果；失败仍保留并展示上次库存。
        self.status_box = tk.Frame(self, bg='#EAF1FC', padx=12, pady=8)
        self.status_box.pack(fill='x', pady=(0, 10))
        self.status_label = tk.Label(self.status_box, textvariable=self.notice, bg='#EAF1FC', fg='#244C87', anchor='w', justify='left', font=('Microsoft YaHei UI', 10))
        self.status_label.pack(fill='x'); wrap_label(self.status_label)
        # 细进度条沿用页面主色，避免系统灰色立体边框打断状态栏。
        ttk.Style(self).configure('Douyin.Horizontal.TProgressbar', background=BLUE, troughcolor='#DDE7F6',
                                  lightcolor=BLUE, darkcolor=BLUE, bordercolor='#DDE7F6', borderwidth=0, thickness=6)
        self.progress = ttk.Progressbar(self.status_box, mode='determinate', maximum=100, style='Douyin.Horizontal.TProgressbar')

        data_card = RoundedCard(self); data_card.pack(fill='both', expand=True)
        data = data_card.content
        data.configure(pady=0)
        data.pack_configure(pady=10)
        toolbar = tk.Frame(data, bg='white'); toolbar.pack(fill='x', pady=(0, 10))
        tk.Label(toolbar, text='搜索', bg='white', fg=INK, font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=(0, 8))
        ttk.Entry(toolbar, textvariable=self.search, width=26).pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.filter_box = ttk.Combobox(toolbar, textvariable=self.filter, state='readonly', width=12,
            values=['全部库存', '低库存预警', 'ERP 缺货', 'ERP 待核对', '库存为零', '有库存', '库存未知'])
        self.filter_box.pack(side='left')
        self.count = tk.Label(toolbar, bg='white', fg=MUTED, width=22, anchor='e')
        self.count.pack(side='right', padx=(8, 0))
        # 使用页签按钮与现有圆角控件统一；隐藏系统凸起边框，保留 Notebook 切页行为。
        tabbar = tk.Frame(data, bg='white'); tabbar.pack(fill='x', pady=(0, 8))
        self.tab_stock = button(tabbar, '整机 SKU 库存', lambda: self.tabs.select(0), primary=True, pady=6)
        self.tab_stock.pack(side='left', padx=(0, 6))
        self.tab_restock = button(tabbar, '补库存提醒', lambda: self.tabs.select(1), pady=6)
        self.tab_restock.pack(side='left', padx=(0, 6))
        self.tab_history = button(tabbar, '库存变化记录', lambda: self.tabs.select(2), pady=6)
        self.tab_history.pack(side='left')
        self.erp_summary = tk.Label(tabbar, bg='white', fg=MUTED, font=('Microsoft YaHei UI', 9))
        self.erp_summary.pack(side='right')
        self.tabs = ttk.Notebook(data, style='Page.TNotebook'); self.tabs.pack(fill='both', expand=True)
        stock_frame, history_frame = tk.Frame(self.tabs, bg='white'), tk.Frame(self.tabs, bg='white')
        self.tabs.add(stock_frame, text='SKU 库存')
        from douyin_restock_ui import RestockPreview
        self.restock = RestockPreview(self.tabs, self)
        self.tabs.add(self.restock, text='补库存提醒')
        self.tabs.add(history_frame, text='库存归零 / 恢复记录')
        self.stock = table(stock_frame, dict(quantity='库存', spot='现货', future='预售', erp='ERP 配件', state='平台状态'),
                           (65, 60, 60, 95, 135))
        # 商品链接作为父行，SKU 作为子行；库存紧挨配置，避免宽表来回滚动。
        self.stock.configure(show='tree headings')
        self.stock.heading('#0', text='整机链接 → 对应 SKU', anchor='w')
        self.stock.column('#0', width=380, minwidth=240, stretch=True)
        style = ttk.Style(self)
        style.configure('Douyin.Treeview', rowheight=32, borderwidth=0)
        self.stock.configure(style='Douyin.Treeview')
        self.stock.bind('<Double-1>', self.show_erp)
        self.stock.bind('<<TreeviewSelect>>', self.select_sku)
        # 空态覆盖表格主体，保留表头，明确区分采集中、未建立基线和筛选无结果。
        self.empty = tk.Frame(self.stock, bg='white', padx=20, pady=18)
        self.empty_title = tk.Label(self.empty, bg='white', fg=INK, font=('Microsoft YaHei UI', 12, 'bold'))
        self.empty_title.pack(pady=(0, 8))
        self.empty_hint = tk.Label(self.empty, bg='white', fg=MUTED, font=('Microsoft YaHei UI', 10), wraplength=420)
        self.empty_hint.pack()
        # 窄窗口使用横向滚动，不把配置名称挤成一个字宽。
        self.history = table(history_frame, dict(time='检测时间', product='商品', name='SKU 配置', kind='变化', quantity='前 → 后', status='通知状态'),
                             (150, 210, 220, 80, 100, 110))
        for column in ('quantity', 'spot', 'future'):
            self.stock.column(column, anchor='e')
            self.stock.heading(column, anchor='e')
        self.stock.tag_configure('zero', foreground='#A85A18')
        self.stock.tag_configure('stripe', background='#F8FAFD')
        self.stock.tag_configure('shortage', foreground='#B54736')
        self.stock.tag_configure('product', background='#EDF3FC', foreground='#244C87', font=('Microsoft YaHei UI', 10, 'bold'))
        self.tabs.bind('<<NotebookTabChanged>>', lambda _: self.refresh_details())
        self.search.trace_add('write', lambda *_: self.refresh_details())
        self.filter.trace_add('write', lambda *_: self.refresh_details())
        footer = tk.Frame(data, bg='white'); footer.pack(side='bottom', fill='x', pady=(8, 0), before=self.tabs)
        self.selected_text = tk.StringVar(self, '选择 SKU 查看编号、缺货配件与处理建议')
        self.selected_label = tk.Label(footer, textvariable=self.selected_text, bg='white', fg=MUTED, anchor='w',
                                      justify='left', font=('Microsoft YaHei UI', 9), height=1)
        self.selected_label.pack(fill='x', pady=(0, 4))
        self.file_button = button(footer, '查看原始表格', self.open_export, padx=10, pady=5)
        self.file_button.pack(side='right')
        self.open_button = button(footer, '打开链接', self.open_product, padx=10, pady=5)
        self.open_button.pack(side='right')
        self.copy_button = button(footer, '复制链接', self.copy_product, padx=10, pady=5)
        self.copy_button.pack(side='right', padx=8)
        self.detail_button = button(footer, 'ERP 缺货明细', self.show_erp, padx=10, pady=5)
        self.detail_button.pack(side='left')
        button(footer, '核对配件库存', lambda: self.action('parts'), padx=10, pady=5).pack(side='left', padx=8)
        self.exclusions_button = button(footer, '下架链接管理', self.exclusions_dialog, padx=10, pady=5)
        self.exclusions_button.pack(side='left')
        self.bind('<Destroy>', self.destroyed)
        self.tick()

    def toggle_settings(self):
        """低频设置可展开，收起后保留原值和当前监控状态。"""
        if self.settings_box.winfo_manager():
            self.settings_box.pack_forget()
            self.settings_button.config(text='监控设置')
        else:
            self.settings_box.pack(fill='x')
            self.settings_button.config(text='收起设置')

    def config(self):
        return self.runtime.store.store(self.ident) if self.ident else None

    def shops_dialog(self):
        """集中查看所有店铺的运行状态，以及各自的维护表和登录设置。"""
        from douyin_shops_ui import ShopsDialog
        return ShopsDialog(self)

    def select_store(self, *_):
        index = self.shop.current()
        if 0 <= index < len(self.shop_rows):
            row = self.shop_rows[index]
            self.ident = row['id']
            self.interval.set(str(row['interval'] // 60))
            self.cache_key = None
            self.refresh_details()

    def refresh_stores(self):
        rows = self.runtime.store.stores()
        names = [r['name'] for r in rows]
        if list(self.shop.cget('values')) != names:
            self.shop.configure(values=names)
        self.shop_rows = rows
        ids = [r['id'] for r in rows]
        if ids and self.ident not in ids:
            self.shop.current(0)
            self.select_store()
        elif self.ident in ids:
            self.shop.current(ids.index(self.ident))
        elif not ids:
            self.ident = None
            self.shop.set('')
            self.cache_key = None
        self.refresh_patrol(rows)

    def layout_patrol(self, _=None):
        """按可用宽度换行，店铺较多或窗口较窄时仍保留完整勾选入口。"""
        width = max(1, self.patrol_box.winfo_width())
        columns = max(1, min(3, width // 230))
        for column in range(3):
            self.patrol_box.columnconfigure(column, weight=1 if column < columns else 0)
        for index, item in enumerate(self.patrol_widgets.values()):
            item['frame'].grid(row=index // columns, column=index % columns, sticky='nsew', padx=(0, 10), pady=3)
            item['check'].config(wraplength=max(140, width // columns - 24))

    def refresh_patrol(self, rows):
        """同步每店勾选与状态，失败店铺不遮住其他店铺的检查进度。"""
        ids = {r['id'] for r in rows}
        for ident in list(self.patrol_widgets):
            if ident not in ids:
                self.patrol_widgets.pop(ident)['frame'].destroy()
        for config in rows:
            ident = config['id']
            if ident not in self.patrol_widgets:
                frame = tk.Frame(self.patrol_box, bg='white')
                value = tk.BooleanVar(self)
                check = tk.Checkbutton(frame, variable=value, text=config['name'], bg='white',
                    activebackground='white', fg=INK, selectcolor='white', anchor='w', justify='left',
                    font=('Microsoft YaHei UI', 9), command=lambda key=ident: self.select_patrol(key))
                check.pack(fill='x')
                status = tk.Label(frame, bg='white', fg=MUTED, anchor='w', justify='left', height=1, width=1,
                                  font=('Microsoft YaHei UI', 9))
                status.pack(fill='x', padx=(22, 0))
                self.patrol_widgets[ident] = dict(frame=frame, value=value, check=check, status=status)
            item = self.patrol_widgets[ident]
            item['value'].set(bool(config['patrol_selected']))
            item['check'].config(text=config['name'])
            status = self.runtime.activity(ident) or config['status']
            item['status'].config(text=status[:28] + ('…' if len(status) > 28 else ''),
                                 fg='#98521A' if any(w in status for w in ('失败','失效','不一致','超时')) else MUTED)
        # 汇总来自当前任务占位，不沿用按钮点击时的队列数量。
        selected = [r for r in rows if r.get('patrol_selected', True)]
        activities = [self.runtime.activity(r['id']) for r in selected]
        waiting = sum('报表' in a for a in activities)
        queued = sum('排队' in a for a in activities)
        running = sum(bool(a) for a in activities) - waiting - queued
        finished = sum(not a and bool(r['checked']) for r, a in zip(selected, activities))
        self.batch_notice.set(f'排队 {queued} 店 · 处理中 {running} 店 · 等待报表 {waiting} 店 · 已有库存 {finished}/{len(selected)} 店')
        self.layout_patrol()

    def select_patrol(self, ident):
        """只保存勾选，不改变库存基线，也不在点击时启动采集。"""
        try:
            self.runtime.store.select_patrol(ident, self.patrol_widgets[ident]['value'].get())
            self.batch_notice.set('')
        except ValueError as error:
            self.batch_notice.set(str(error))
        self.refresh_stores(); self.refresh_details()

    def add_dialog(self):
        win = tk.Toplevel(self); win.title('添加监控店铺'); win.configure(bg=BG)
        win.transient(self.winfo_toplevel()); win.resizable(False, False)
        card = RoundedCard(win); card.pack(fill='both', expand=True, padx=16, pady=16)
        name = tk.StringVar(win)
        tk.Label(card.content, text='店铺名称须与抖店后台一致', bg='white', fg=INK).pack(anchor='w', pady=(0, 10))
        entry = ttk.Entry(card.content, textvariable=name, width=32); entry.pack(fill='x')
        tk.Label(card.content, text='添加后连接店铺，完成首轮官方导出建立基线。', bg='white', fg=MUTED).pack(anchor='w', pady=12)
        def save():
            try:
                self.ident = self.runtime.store.add_store(name.get(), interval=self.minutes() * 60)
                self.cache_key = None
                self.refresh_stores(); self.refresh_details(); win.destroy()
            except ValueError as error:
                messagebox.showerror('未添加', str(error), parent=win)
        button(card.content, '添加店铺', save, primary=True).pack(anchor='e')
        entry.focus_set()

    def minutes(self):
        try:
            minutes = int(self.interval.get())
        except ValueError:
            raise ValueError('请输入 5–1440 的整数分钟。') from None
        if not 5 <= minutes <= 1440:
            raise ValueError('请输入 5–1440 的整数分钟。')
        return minutes

    def save_interval(self):
        if not self.ident:
            return
        try:
            self.runtime.submit('interval:' + str(self.minutes() * 60), self.ident)
        except ValueError as error:
            messagebox.showerror('未保存', str(error), parent=self)

    def export(self):
        """同一次操作覆盖勾选店铺，首次身份核验由各店自己的后台队列完成。"""
        result = self.runtime.check_all()
        text = f"已排队 {result['queued']} 店，已在检查 {result['busy']} 店。"
        if result['skipped']:
            text += '未就绪：' + '、'.join(result['skipped'])
        if not any(r['patrol_selected'] for r in self.runtime.store.stores()):
            text = '请先勾选至少一家参与巡检的店铺。'
        self.batch_notice.set(text)
        self.refresh_stores(); self.refresh_details()

    def toggle(self):
        result = self.runtime.toggle_selected()
        text = f"已提交{'启用' if result['enabled'] else '暂停'}所选店铺定时监控：{result['queued']} 店。"
        if result['skipped']:
            text += '暂未启用：' + '、'.join(result['skipped'])
        self.batch_notice.set(text)
        self.refresh_details()

    def action(self, operation):
        config = self.config()
        if not config:
            return
        if operation == 'confirm' and not messagebox.askyesno('建立库存基线',
                f"确认 所选浏览器中的店铺是“{config['name']}”？\n首次导出只建立基线，不发送群消息。", parent=self):
            return
        self.runtime.submit(operation, self.ident)
        self.notice.set('操作已排队，正在处理…')

    def help(self):
        messagebox.showinfo('抖店库存监控',
            '仅巡检 DIY 整机类目，单品不参与。\n按商品链接分组，展开后查看对应 SKU 与库存。\n库存＝现货可售＋预售库存＋阶梯库存；任一项未知则库存未知。\n导出表不含 SKU 上下架状态；库存归零不等于前台售罄。\n这里是商家库存状态，不代表所有地区的买家可购买性。\n\n配置：读取本店自行选择的主机维护表，按商品ID＋SKU ID匹配配件。\n库存：只读 ERP 公司大库实时可销库存，不查询 ERP 套餐。\n待入不计入可组装量，共用配件不能跨配置重复分配。\n双击 SKU 查看缺货配件与待入数量。\n\n补库存提醒：按预警规则汇总零库存且配件齐全的 SKU，按店铺和链接展示。\n首次检查或缺货配件补齐时均会生成；相同结果不重复记录。\n在“补库存提醒”中预览或复制 Markdown；群消息强制关闭。\n官方表为主数据源；按四级类目筛整机，不逐链接展开。\nChrome 或 Edge 均可。推荐独立店铺浏览器：打开本店窗口，手动登录后校验连接，无需扩展标识；已有扩展模式仍可使用。\n每三天清理三天前的历史导出至回收站，保留最新有效表。', parent=self)

    def selected_product_url(self):
        selected = self.stock.selection()
        if not selected:
            return ''
        # 父行和 SKU 子行均可定位同一真实商品链接。
        ident = selected[0]
        pid = ident[8:] if ident.startswith('product:') else ident.split(':', 1)[0]
        return product_url(pid)

    def open_product(self):
        url = self.selected_product_url()
        if url:
            webbrowser.open(url)

    def copy_product(self):
        url = self.selected_product_url()
        if url:
            self.clipboard_clear(); self.clipboard_append(url)
            self.notice.set('已复制所选整机商品链接。')

    def maintenance_settings(self):
        # 文件选择只保存在当前店铺，解析在后台执行。
        from douyin_settings_ui import maintenance_dialog
        return maintenance_dialog(self)

    def exclusions_dialog(self):
        """管理当前店铺的人工排除名单，不操作抖店上下架。"""
        if not self.config():
            self.notice.set('请先选择监控店铺。'); return
        from douyin_exclusions_ui import ExclusionsDialog
        return ExclusionsDialog(self)

    def login_settings(self):
        from douyin_settings_ui import login_dialog
        return login_dialog(self)

    def show_erp(self, _=None):
        selected = self.stock.selection()
        row = next((r for r in self.cached_rows if r['product_id'] + ':' + r['sku_id'] in selected), None)
        if not row:
            return
        erp = row.get('erp', {})
        checked = erp.get('checked')
        lines = [row['sku_name'], row.get('stock_state', ''), 'ERP：' + erp.get('state', '未核对'),
                 '配置来源：主机维护表（已保存内容）',
                 '维护表：' + str(erp.get('workbook') or '未配置'),
                 '工作表／行：' + str(erp.get('sheet') or '—') + ' / ' + str(erp.get('row') or '—'),
                 '库存仓库：公司大库', '核对时间：' + (datetime.fromtimestamp(checked).strftime('%m-%d %H:%M:%S') if checked else '未核对')]
        for part in erp.get('parts', []):
            lines.append(f"{part['name']}（{part['goods_id']}）：每台需 {part['required']}，可销 {part['available'] if part['available'] is not None else '未知'}")
        for part in erp.get('shortages', []):
            incoming = '未知' if part.get('incoming') is None else str(part['incoming'])
            lines.append(f"{part['name']}（{part['goods_id']}）：每台需 {part['required']}，可销 {part['available']}，缺 {part['shortage']}，待入 {incoming}")
        lines.extend(erp.get('unknown', []))
        lines.append('可组装量只针对当前配置；不代表共用配件可以重复分配。')
        messagebox.showinfo('SKU 缺货核对明细', '\n\n'.join(lines), parent=self)

    def select_sku(self, _=None):
        """长编号与配件原因放到选中明细，主表优先保留链接、SKU 和库存。"""
        selected = self.stock.selection()
        row = next((r for r in self.cached_rows if r['product_id'] + ':' + r['sku_id'] in selected), None)
        self.detail_button.config(state='normal' if row else 'disabled')
        for control in (self.open_button, self.copy_button):
            control.config(state='normal' if selected else 'disabled')
        if row:
            erp = row.get('erp', {})
            detail = '；'.join(f"{p['name']} 缺 {p['shortage']}" for p in erp.get('shortages', []))
            self.selected_text.set(f"SKU {row['sku_id']}  ·  {erp.get('action') or erp.get('state', 'ERP 未核对')}" + ('  ·  ' + detail if detail else ''))
        elif selected:
            self.selected_text.set(self.selected_product_url())
        else:
            self.selected_text.set('选择 SKU 查看编号、缺货配件与处理建议；双击 SKU 打开完整明细。')

    def open_export(self):
        config = self.config()
        if config and config.get('export_file') and Path(config['export_file']).is_file():
            # 仅在用户主动点击时打开文件，不在采集后弹出 Excel。
            os.startfile(config['export_file'])

    def refresh_details(self):
        config = self.config()
        if config:
            key = (self.ident, config['checked'], config['revision'])
            if key != self.cache_key:
                self.cached_rows = self.runtime.store.snapshots(self.ident)
                self.cache_key = key
            checked = datetime.fromtimestamp(config['checked']).strftime('%m-%d %H:%M') if config['checked'] else '尚未巡检'
            self.source_label.config(text=('维护表：' + Path(config['maintenance_path']).name + ' · ' + config['maintenance_sheet']) if config['maintenance_path'] else '配件来源未设置：监控设置 → 主机维护表 → 选择文件')
            timer = '未勾选巡检' if not config['patrol_selected'] else '定时监控中' if config['enabled'] else '定时已暂停'
            self.summary.config(text=self.batch_notice.get() or f"官方导出 · 仅整机  ·  {timer}  ·  每 {config['interval']//60} 分钟  ·  最近有效库存 {checked}")
            self.export_button.config(text='立即检查', state='normal')
            path = config.get('export_file') or ''
            self.export_path.set(('原始表格：' + Path(path).name) if path else '首份导出后可查看原始 Excel')
            self.file_button.config(state='normal' if path and Path(path).is_file() else 'disabled')
            status = config['status']
        else:
            self.cached_rows = []
            self.source_label.config(text='每店单独设置维护表与浏览器登录')
            self.summary.config(text='先添加店铺，再连接店铺 建立库存基线。')
            self.export_button.config(state='disabled'); self.toggle_button.config(state='disabled')
            self.file_button.config(state='disabled'); self.export_path.set('尚未导出')
            status = '还没有监控店铺。点击“添加店铺”开始。'
        selected = [s for s in self.shop_rows if s.get('patrol_selected', True)]
        any_enabled = any(s['enabled'] for s in selected)
        self.toggle_button.config(text='暂停所选定时' if any_enabled else '启用所选定时',
                                  state='normal' if selected else 'disabled')
        # 已在运行的店铺通过后台去重，不禁用全局按钮，以便加入其他勾选店铺。
        self.export_button.config(state='normal' if selected else 'disabled')
        # 当前结果保持所查看店铺的提示，多店状态显示在上方勾选区。
        queued = self.runtime.activity(self.ident) if self.ident else ''
        self.notice.set(queued or status)
        failed = any(word in status for word in ('失败', '失效', '不一致', '未采用', '超时'))
        busy = '正在' in status or '页面已读完' in status or bool(queued)
        progress = re.search(r'(\d+)/(\d+) 个链接', status)
        if busy:
            self.progress.configure(value=(int(progress[1]) / max(1, int(progress[2])) * 100) if progress else 100)
            self.progress.pack(fill='x', pady=(7, 0))
        else:
            self.progress.pack_forget()
        color, ink = ('#FFF3E7', '#98521A') if failed else ('#EAF1FC', '#244C87')
        self.status_box.config(bg=color); self.status_label.config(bg=color, fg=ink)
        needle = self.search.get().strip().casefold()
        scoped = machine_rows(self.cached_rows)
        if self.cached_rows and not scoped and not busy and not failed:
            self.notice.set('历史库存尚未核验整机类目，请点击“立即检查”建立整机库存列表。')
        rows = [r for r in scoped if needle in ' '.join(str(r.get(k,'')) for k in ('product_name','sku_name','product_id','sku_id')).casefold()]
        from douyin_low_stock import is_low
        rules = self.runtime.store.low_stock_rules(self.ident) if self.ident else {'default':1,'parts':{}}
        choice = self.filter.get()
        rows = [r for r in rows if choice == '全部库存' or (choice == '低库存预警' and is_low(r, rules)) or (choice == '库存为零' and r['quantity'] == 0)
                or (choice == 'ERP 缺货' and r.get('erp', {}).get('state') == '缺货')
                or (choice == 'ERP 待核对' and r.get('erp', {}).get('state', '待核对') in ('待核对', '未核对'))
                or (choice == '有库存' and r['quantity'] is not None and r['quantity'] > 0)
                or (choice == '库存未知' and r['quantity'] is None)]
        fmt = lambda value: '—' if value is None else value
        signature = (self.cache_key, needle, choice)
        if signature != getattr(self, '_stock_signature', None):
            # 只在数据或筛选变化时重建，保留用户折叠状态和选择，定时刷新不跳动。
            roots = self.stock.get_children()
            opened = {pid: self.stock.item(pid, 'open') for pid in roots}
            selected = self.stock.selection()
            if roots:
                self.stock.delete(*roots)
            for index, row in enumerate(rows):
                parent = 'product:' + row['product_id']
                if not self.stock.exists(parent):
                    self.stock.insert('', 'end', iid=parent, text=row['product_name'].replace('\n', ' '),
                                      tags=('product',), open=opened.get(parent, True))
                components, erp = row.get('components', {}), row.get('erp', {})
                values = (fmt(row['quantity']), fmt(components.get('现货可售')), fmt(components.get('预售库存')),
                          erp.get('state', '未核对'), row.get('stock_state', '售卖状态未核对'))
                tags = (('stripe',) if index % 2 else ()) + (('shortage',) if erp.get('state') == '缺货' else ('zero',) if is_low(row, rules) else ())
                self.stock.insert(parent, 'end', iid=row['product_id'] + ':' + row['sku_id'], text=row['sku_name'].replace('\n', ' '),
                                  values=values, tags=tags)
            self.stock.selection_set([iid for iid in selected if self.stock.exists(iid)])
            self._stock_signature = signature
            self.select_sku()
        shortage = sum(r.get('erp', {}).get('state') == '缺货' for r in scoped)
        unknown = sum(r.get('erp', {}).get('state', '待核对') in ('待核对', '未核对') for r in scoped)
        self.erp_summary.config(text=f'ERP 缺货 {shortage}  ·  待核对 {unknown}' if scoped else '仅监控整机 · 单品已排除',
                                fg='#B54736' if shortage else MUTED)
        if rows:
            self.empty.place_forget()
        else:
            self.empty_title.config(text='正在读取整机库存' if busy else '没有符合条件的 SKU' if scoped else '等待首次整机巡检')
            self.empty_hint.config(text='采集完成后，商品链接和 SKU 库存会自动显示。' if busy else
                                   '试试清空搜索或切换到“全部库存”。' if scoped else
                                   '点击“立即检查”建立库存列表；采集失败不会被当成零库存。')
            self.empty.place(relx=.5, rely=.55, anchor='center')
        labels = {'pending':'待发送','sent':'已发送','failed':'等待重试','partial':'部分送达','unknown':'待核实','skipped':'已跳过','cancelled':'已取消','preview':'仅记录'}
        active = self.tabs.index('current')
        events = [r for r in machine_rows(self.runtime.store.events() if active == 2 else []) if r['store_id'] == self.ident
                  and needle in ' '.join(str(r.get(k,'')) for k in ('product_name','sku_name','product_id','sku_id')).casefold()]
        if active == 2:
            sync_tree(self.history, [(r['id'], (datetime.fromtimestamp(r['observed']).strftime('%m-%d %H:%M:%S'), r['product_name'], r['sku_name'],
                 '库存恢复' if r.get('kind') == 'restocked' else '库存归零', f"{r['previous']} → {r['quantity']}", labels.get(r['status'],r['status'])), ()) for r in events])
        active = self.tabs.index('current')
        if active == 1:
            self.restock.refresh()
        for index, tab in enumerate((self.tab_stock, self.tab_restock, self.tab_history)):
            tab._primary = index == active
            tab.config(bg=BLUE if index == active else 'white', fg='white' if index == active else INK,
                       activeforeground='white' if index == active else INK)
        self.filter_box.config(state='readonly' if active == 0 else 'disabled')
        self.count.config(text=f'{len(events)} 条记录' if active == 2 else '按当前店铺汇总 · 通知状态见下方' if active == 1 else
                          f'{len({r["product_id"] for r in rows})} 个链接 · {len(rows):,} 个 SKU')

    def tick(self):
        try:
            # 隐藏页面无需轮询历史表或重绘，后台任务仍独立运行。
            if self.shop_rows and not self.winfo_ismapped():
                return
            self.refresh_stores(); self.refresh_details()
            cleanup = getattr(self.runtime, 'cleanup_state', {})
            self.cleanup_label.config(text=('；'.join(cleanup['errors'][:1]) if cleanup.get('errors') else
                                           '历史导出每 3 天清理一次：移入回收站，保留最新有效表。'))
            ready = self.runtime.notification_ready()
            self.notification.config(text='群提醒：已启用' if ready else '群提醒：关闭', fg='#087B54' if ready else MUTED)
        except (ValueError, tk.TclError):
            self.notice.set('页面刷新未完成，请重新选择店铺。')
        finally:
            self.after_id = self.after(2000, self.tick)

    def destroyed(self, event):
        if event.widget is self and self.after_id:
            self.after_cancel(self.after_id)
            self.after_id = None
