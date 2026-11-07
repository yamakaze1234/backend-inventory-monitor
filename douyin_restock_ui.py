"""补库存机器人文案的本地预览与历史记录；没有发送按钮。"""
import tkinter as tk
import webbrowser
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from douyin_restock import stamp
from inventory_theme import INK, MUTED, button


class RestockPreview(tk.Frame):
    def __init__(self, parent, owner):
        super().__init__(parent, bg='white')
        self.owner, self.ident, self.records, self.current = owner, None, [], None
        line = tk.Frame(self, bg='white'); line.pack(fill='x', pady=(4, 8))
        self.choice = ttk.Combobox(line, state='readonly', width=40)
        self.choice.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.choice.bind('<<ComboboxSelected>>', lambda _: self.render())
        self.copy = button(line, '复制 Markdown', self.copy_markdown, padx=10, pady=5)
        self.copy.pack(side='right')
        self.open = button(line, '在线明细', self.open_online, padx=10, pady=5)
        self.open.pack(side='right', padx=6)
        button(line, '同步明细', lambda: owner.action('online'), padx=10, pady=5).pack(side='right')
        self.summary = tk.Label(self, bg='white', fg=MUTED, anchor='w', justify='left')
        self.summary.pack(fill='x', pady=(0, 8))
        self.editor = ScrolledText(self, wrap='word', relief='flat', bg='#F8FAFD', fg=INK,
                                   font=('Microsoft YaHei UI', 10), padx=12, pady=10)
        self.editor.pack(fill='both', expand=True)
        self.editor.config(state='disabled')
        self.text, self.signature = '', None

    def refresh(self):
        """切店铺时回到本店当前文案，定时刷新不打断历史回看。"""
        ident = self.owner.ident
        selected = self.choice.current() if ident == self.ident else 0
        selected_id = self.records[selected - 1]['id'] if 0 < selected <= len(self.records) else None
        self.ident = ident
        self.current = self.owner.runtime.store.restock_preview(ident) if ident else None
        self.records = self.owner.runtime.store.restock_records(ident) if ident else []
        self.open.config(state='normal' if (self.owner.config() or {}).get('online_url') else 'disabled')
        labels = ['当前补库存清单'] + [f"{stamp(r['detected_at'])} · {r['product_count']} 链接 / {r['sku_count']} SKU · 仅记录" for r in self.records]
        self.choice.config(values=labels)
        selected = next((i for i, r in enumerate(self.records, 1) if r['id'] == selected_id), 0)
        self.choice.current(selected)
        self.render()

    def render(self):
        selected = self.choice.current()
        data = self.records[selected - 1] if 0 < selected <= len(self.records) else self.current
        from douyin_restock import brief
        payload = brief(data, (self.owner.config() or {}).get('online_url','')) if data else None
        self.text = payload['markdown']['text'] if payload else ''
        if self.text:
            summary = ('历史记录（当时结果）' if selected > 0 else '当前符合条件') + f"：{data['product_count']} 个链接 · {data['sku_count']} 个 SKU"
            body = self.text
        else:
            summary = '零库存且配件齐全的 SKU 可补库存；阈值见预警规则'
            body = '当前没有符合条件的补库存提醒。\n\n先为本店选择主机维护表，再完成官方导出或点击“核对配件库存”。\n缺货、待核对、库存未知及超过预警阈值的 SKU 不进入补库存提醒。'
        if data and data.get('excluded_count'):
            summary += f"；手动排除 {data['excluded_count']} 个链接"
        if self.ident and selected == 0:
            from douyin_notification import delivery_label
            from notification_robots import store_for_service
            runtime = self.owner.runtime
            summary += ('；群提醒：' + delivery_label(runtime.store, self.ident, store_for_service(runtime.service))
                        if runtime.notification_ready() else '；群提醒：关闭')
        self.summary.config(text=summary)
        if self.ident:
            from douyin_online import status
            online = status(self.owner.runtime.store.root, self.ident)
            if online.get('message'):
                self.summary.config(text=summary + '\n' + online['message'])
        self.copy.config(state='normal' if self.text else 'disabled')
        signature = (self.ident, selected, body)
        if signature != self.signature:
            self.editor.config(state='normal'); self.editor.delete('1.0', 'end'); self.editor.insert('1.0', body)
            self.editor.config(state='disabled'); self.signature = signature

    def copy_markdown(self):
        if self.text:
            self.clipboard_clear(); self.clipboard_append(self.text)
            self.summary.config(text='已复制 Markdown；未发送群消息')

    def open_online(self):
        """仅用户点击时在浏览器打开在线表，不自动弹出预览。"""
        url = (self.owner.config() or {}).get('online_url')
        if url:
            webbrowser.open(url)
