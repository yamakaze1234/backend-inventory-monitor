"""ERP unit-price monitor controls; business files remain local and read-only."""
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from inventory_theme import BG, INK, MUTED, BORDER, RoundedCard, ScrollBar, button, wrap_label


class ProfitPanel(tk.Frame):
    def __init__(self, master, monitor):
        super().__init__(master, bg=BG)
        self.monitor = monitor
        self.path, self.interval = tk.StringVar(self), tk.StringVar(self)
        self.enabled, self.notify = tk.BooleanVar(self), tk.BooleanVar(self)
        self.show_all_refs = tk.BooleanVar(self, False)
        self.message = tk.StringVar(self)
        # 配置与结果分卡显示，按钮和输入框复用全局组件。
        card = RoundedCard(self); card.pack(fill='x', pady=(0, 12))
        content = card.content
        tk.Label(content, text='监控范围', bg='white', fg=INK, font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        hint = tk.Label(content, text='绑定配置文件后监控对应配件；首次建立基准，累计涨跌达到 100 元时提醒。', bg='white', fg=MUTED, anchor='w')
        hint.pack(fill='x', pady=(6, 10)); wrap_label(hint)
        row = tk.Frame(content, bg='white'); row.pack(fill='x')
        ttk.Entry(row, textvariable=self.path).pack(side='left', fill='x', expand=True)
        button(row, '选择配置文件', self.choose, padx=10).pack(side='left', padx=6)
        button(row, '绑定', self.bind, primary=True, padx=12).pack(side='left')
        button(row, '解除', self.unbind, padx=12).pack(side='left', padx=(6, 0))
        controls = tk.Frame(content, bg='white'); controls.pack(fill='x', pady=(10, 0))
        ttk.Checkbutton(controls, text='定时检测', variable=self.enabled, command=self.toggle).pack(side='left')
        tk.Label(controls, text='每', bg='white', fg=MUTED).pack(side='left', padx=(14, 4))
        ttk.Spinbox(controls, from_=10, to=10080, width=5, textvariable=self.interval).pack(side='left')
        tk.Label(controls, text='分钟', bg='white', fg=MUTED).pack(side='left', padx=4)
        button(controls, '保存', self.save_interval, padx=10).pack(side='left', padx=6)
        button(controls, '立即检测', self.check, primary=True, padx=12).pack(side='right')
        ttk.Checkbutton(content, text='发送 ERP 单价变化群提醒', variable=self.notify, command=self.toggle_notify).pack(anchor='w', pady=(8, 0))
        self.recipients = tk.Label(content, bg='white', fg=MUTED, anchor='w', justify='left', font=('Microsoft YaHei UI', 9))
        self.recipients.pack(fill='x'); wrap_label(self.recipients)
        status = tk.Label(self, textvariable=self.message, bg=BG, fg='#98521A', anchor='w', justify='left')
        status.pack(fill='x', pady=(0, 8)); wrap_label(status)
        results = RoundedCard(self); results.pack(fill='both', expand=True)
        header = tk.Frame(results.content, bg='white'); header.pack(fill='x', pady=(0, 8))
        tk.Label(header, text='检测结果', bg='white', fg=INK, font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        ttk.Checkbutton(header, text='展开全部受影响配置', variable=self.show_all_refs, command=self.refresh).pack(side='right')
        box = tk.Frame(results.content, bg='white'); box.pack(fill='both', expand=True)
        self.summary = tk.Text(box, wrap='word', height=6, state='disabled', font=('Microsoft YaHei UI', 10),
                               bg='white', fg=INK, relief='flat', borderwidth=0, padx=8, pady=8)
        scroll = ScrollBar(box, command=self.summary.yview)
        self.summary.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.summary.pack(fill='both', expand=True)
        self.refresh()

    def choose(self):
        path = filedialog.askopenfilename(parent=self, title='选择工作台发布的 manifest.json', filetypes=[('JSON', '*.json')])
        if path:
            self.path.set(path)
            self.preview()

    def preview(self):
        try:
            result = self.monitor.preview(self.path.get())
            self.message.set(f"待绑定：{result['count']} 套配置 · {result['goodsCount']} 个 ERP 商品 ID · 店铺 {', '.join(result['shops'])} · 来源 {result['sourceId']} · 修订 {result['revision']}")
        except Exception as error:
            self.message.set(str(error))

    def bind(self):
        try:
            preview = self.monitor.preview(self.path.get())
            text = (f"来源：{preview['sourceId']}\n工作区：{preview['workspaceId']}\n"
                    f"修订：{preview['revision']}\n店铺：{', '.join(preview['shops'])}\n"
                    f"配置：{preview['count']} 套 · ERP 商品 ID：{preview['goodsCount']} 个\n"
                    f"无效 ERP ID 行：{preview['invalidIds']} 条\n发布时间：{preview['publishedAt']}\n\n"
                    '首次有效价仅建立基准，不发送变化提醒。绑定这份完整快照？')
            if not messagebox.askyesno('预览联动范围', text, parent=self):
                return
            result = self.monitor.bind(self.path.get())
            self.message.set(f"已绑定 {result['goodsCount']} 个 ERP 商品 ID；关闭配置工作台后仍使用本快照。")
            self.refresh()
        except Exception as error:
            self.message.set(str(error))

    def unbind(self):
        if not messagebox.askyesno('解除旧绑定', '将停止定时检测与 ERP 单价通知，保留历史结果和回执。确认解除？', parent=self):
            return
        try:
            self.monitor.unbind()
            self.path.set('')
            self.message.set('已解除旧来源，可预览绑定新快照。')
            self.refresh()
        except Exception as error:
            self.message.set(str(error))

    def toggle(self):
        try:
            self.monitor.set_enabled(self.enabled.get())
            self.message.set('定时检测已开启' if self.enabled.get() else '定时检测已暂停')
        except Exception as error:
            self.enabled.set(False)
            self.message.set(str(error))

    def save_interval(self):
        try:
            self.monitor.set_interval(self.interval.get())
            self.message.set('检测周期已保存')
        except Exception as error:
            self.message.set(str(error))

    def toggle_notify(self):
        try:
            if self.notify.get():
                preview = self.monitor.notification_preview()
                text = '接收者：' + '、'.join(preview['recipients']) + '\n\n消息预览：\n' + preview['message'][:2500]
                if not messagebox.askyesno('ERP 单价提醒预览', text, parent=self):
                    self.notify.set(False)
                    return
            self.monitor.set_notifications(self.notify.get())
            self.message.set('ERP 单价通知已开启' if self.notify.get() else 'ERP 单价通知已关闭')
        except Exception as error:
            self.notify.set(False)
            self.message.set(str(error))

    def check(self):
        self.message.set('正在读取最新快照并检测 ERP 单价…')
        def worker():
            try:
                run_id = self.monitor.check()
                message = f'检测完成：{run_id}' if run_id else '上一轮仍在执行'
            except Exception as error:
                message = str(error)
            self.after(0, lambda: (self.message.set(message), self.refresh()))
        threading.Thread(target=worker, daemon=True, name='profit-panel-check').start()

    def refresh(self):
        status = self.monitor.status()
        self.path.set(status['path'] or self.path.get())
        self.interval.set(str(status['intervalSeconds'] // 60))
        self.enabled.set(status['enabled'])
        self.notify.set(status['notify'])
        names = [r['name'] for r in self.monitor.robots.list_robots() if r['enabled'] and 'profit' in r['channels']] if self.monitor.robots else []
        self.recipients.config(text='ERP 单价提醒接收者：' + ('、'.join(names) if names else '尚未选择；请在“设置 → 通知机器人”勾选 ERP 单价累计涨跌 100 元通道'))
        lines = [f"当前快照：{status['count']} 套 · {status['goodsCount']} 个 ERP 商品 ID · 修订 {status['revision']} · 发布 {status['publishedAt'] or '未知'}",
                 f"无效 ERP ID 行：{status['invalidIds']} 条；首次有效价仅建立基准，累计涨跌达到 100 元提醒",
                 f"上次成功读文件：{status['lastReadAt'] or '尚无'}",
                 f"来源异常：{status['sourceError'] or '无'}",
                 f"上轮检测：{status['lastRun']['status'] if status['lastRun'] else '尚无'}",
                 f"近期异常事件：{len(status['events'])} 条"]
        if not status['count']:
            lines = ['尚未绑定配置文件。', '', '点击“选择配置文件”并绑定后，首次检测会建立价格基准。',
                     '后续在这里查看配件单价变化及受影响配置。']
        for row in status['lastResults']:
            value = f"{row['unit_cents']/100:.2f} 元" if row['valid'] else '无法采集'
            anchor = row['anchor_cents']
            delta = row['anchor_delta_cents']
            comparison = (f" · 比较基准 {anchor/100:.2f} 元 · 累计"
                          f"{'涨' if delta > 0 else '跌' if delta < 0 else '持平'} {abs(delta)/100:.2f} 元") if anchor is not None and delta is not None else ''
            state = row['reason'] or ('达到 100 元，已建立新基准' if row['changed'] else
                                      '首次基准已建立' if row['valid'] and anchor is None else '未达提醒阈值')
            lines.append(f"goods_id {row['goods_id']} / {row['name']} · ERP 当前单价 {value}{comparison} · 影响 {row['affectedConfigs']} 套配置 · {state}")
            if row['changed']:
                refs = row['refs']
                shown = refs if self.show_all_refs.get() else refs[:5]
                lines.extend(f"  {shop} / {product} / {name} / {slot}" for shop, _, product, name, slot in shown)
                if len(refs) > len(shown):
                    lines.append(f"  另 {len(refs)-len(shown)} 条关联，勾选“展开全部受影响配置”查看")
        self.summary.config(state='normal')
        self.summary.delete('1.0', 'end')
        self.summary.insert('1.0', '\n'.join(lines))
        self.summary.config(state='disabled')
