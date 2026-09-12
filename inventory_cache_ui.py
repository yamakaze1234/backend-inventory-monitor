"""Local cache cleanup controls. Worker threads communicate only through a queue."""
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from inventory_theme import INK, MUTED, ScrollBar, button, section


def _preview(service, days):
    from inventory_cache import preview
    return preview(service, days=days)


def _execute(service, plan):
    from inventory_cache import execute
    return execute(service, plan)


def _work(events, operation, service, value):
    try:
        result = (_preview if operation == 'preview' else _execute)(service, value)
        events.put((operation, result))
    except Exception:
        events.put(('failed', operation))


def _size(value):
    size = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:,.1f} {unit}'
        size /= 1024


class CacheSection(tk.Frame):
    def __init__(self, parent, service):
        super().__init__(parent, bg='white')
        self.service = service
        self.events = Queue()
        self.running = False
        self.closed = False
        self.plan = None
        self.poll_id = None
        self.days = tk.StringVar(self, '30天前')
        self.notice = tk.StringVar(self, '先预览清理范围，再确认清理。')
        row = tk.Frame(self, bg='white')
        row.pack(fill='x')
        tk.Label(row, text='清理范围', bg='white', fg=INK).pack(side='left', padx=(0, 8))
        self.picker = ttk.Combobox(row, textvariable=self.days, values=('7天前', '30天前', '90天前', '全部'), state='readonly', width=10)
        self.picker.pack(side='left', padx=(0, 12))
        self.preview_button = button(row, '预览清理范围', self.preview)
        self.preview_button.pack(side='left', padx=(0, 8))
        self.clean_button = button(row, '确认清理', self.clean, state='disabled')
        self.clean_button.pack(side='left')
        explanation = tk.Label(self, text='保留配置、登录信息、Markdown 报告和报告防重记录；待发送及状态未知记录保留。\n数据库记录先导出并放入回收站，再清理；缓存文件仅移入回收站。\n不会清理钉钉服务器历史，后续报告仍可重新拉取历史。', bg='white', fg=MUTED, justify='left', anchor='w')
        explanation.pack(fill='x', pady=(12, 8))
        explanation.bind('<Configure>', lambda event: explanation.configure(wraplength=max(1, event.width)))
        box = tk.Frame(self, bg='white')
        box.pack(fill='both', expand=True)
        self.details = tk.Text(box, height=8, wrap='none', state='disabled', bg='#F8FAFC', fg=INK, relief='flat')
        vertical = ScrollBar(box, orient='vertical', command=self.details.yview, bg='#F8FAFC')
        horizontal = ScrollBar(box, orient='horizontal', command=self.details.xview, bg='#F8FAFC')
        self.details.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.details.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        status = tk.Label(self, textvariable=self.notice, bg='white', fg=MUTED, justify='left', anchor='w')
        status.pack(fill='x', pady=(8, 0))
        status.bind('<Configure>', lambda event: status.configure(wraplength=max(1, event.width)))
        self.days.trace_add('write', self._range_changed)
        self.bind('<Destroy>', self._on_destroy, add='+')

    def _text(self, text):
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', text)
        self.details.configure(state='disabled')

    def _range_changed(self, *args):
        if self.closed:
            return
        self.plan = None
        self.clean_button.configure(state='disabled')
        self._text('')
        self.notice.set('范围已变化，请重新预览。')

    def _start(self, operation, value):
        self.running = True
        self.plan = None
        self.preview_button.configure(state='disabled')
        self.clean_button.configure(state='disabled')
        self.picker.configure(state='disabled')
        self.notice.set('正在预览…' if operation == 'preview' else '正在清理，请等待结果…')
        try:
            threading.Thread(target=_work, args=(self.events, operation, self.service, value), daemon=True).start()
        except RuntimeError:
            self.events.put(('failed', operation))
        self.poll_id = self.after(100, self._poll)

    def preview(self):
        if self.running or self.closed:
            return
        days = {'7天前': 7, '30天前': 30, '90天前': 90, '全部': 0}[self.days.get()]
        self._text('')
        self._start('preview', days)

    def clean(self):
        if self.running or self.closed or self.plan is None:
            return
        plan = self.plan
        if not (plan.get('row_count', 0) or plan.get('file_count', 0)):
            return
        summary = f"范围：{self.days.get()}\n数据库记录：{plan.get('row_count', 0):,} 条\n缓存文件：{plan.get('file_count', 0):,} 个\n数据量：{_size(plan.get('total_bytes', 0))}（不保证释放同等磁盘空间）\n\n按上述范围清理，并将备份和文件放入回收站？"
        if messagebox.askyesno('确认清理本地缓存', summary, parent=self):
            if not self.closed and self.plan is plan and not self.running:
                self._start('execute', plan)

    def _poll(self):
        self.poll_id = None
        if self.closed:
            return
        try:
            kind, value = self.events.get_nowait()
        except Empty:
            self.poll_id = self.after(100, self._poll)
            return
        self.running = False
        self.preview_button.configure(state='normal')
        self.picker.configure(state='readonly')
        if kind == 'preview':
            self.plan = value
            cutoff = value.get('cutoff')
            if isinstance(cutoff, (float, int)):
                cutoff = datetime.fromtimestamp(cutoff, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            lines = [f"范围：{self.days.get()}；截止（北京时间）：{cutoff or '不限时间'}", '']
            for group in value.get('groups', []):
                lines.append(f"{group['label']}：{group.get('rows', 0):,} 条；{_size(group.get('bytes', 0))}")
            lines.extend(['', '缓存文件（可横向滚动查看完整路径）：'])
            for item in value.get('files', []):
                lines.append(f"{item['path']}  |  {_size(item.get('size', 0))}")
            self._text('\n'.join(lines))
            rows, files = value.get('row_count', 0), value.get('file_count', 0)
            self.notice.set(f"共 {rows:,} 条记录、{files:,} 个文件，数据量 {_size(value.get('total_bytes', 0))}。数据量不保证等于磁盘释放量。" if rows or files else '该范围没有可清理的数据。')
            self.clean_button.configure(state='normal' if rows or files else 'disabled')
        elif kind == 'execute':
            errors = value.get('errors', [])
            skipped = value.get('skipped', 0)
            if isinstance(skipped, (list, tuple)):
                skipped = len(skipped)
            self.notice.set(f"实际清理 {value.get('deleted_rows', 0):,} 条记录，回收 {value.get('recycled_files', 0):,} 个文件；跳过 {skipped} 项，失败 {len(errors)} 项。请重新预览以查看剩余数据。")
            self._text('\n'.join(errors) if errors else '清理操作已结束，以上为实际处理数量。')
        else:
            self.notice.set('预览失败，请重试。' if value == 'preview' else '清理未完成，可能已有部分数据处理；请重新预览后检查。')

    def _on_destroy(self, event):
        if event.widget is self:
            self.closed = True
            if self.poll_id is not None:
                self.after_cancel(self.poll_id)
                self.poll_id = None


def build_section(parent, service):
    card = section(parent, '本地缓存清理', '先预览，再确认；默认只处理 30 天前的缓存。')
    panel = CacheSection(card, service)
    panel.pack(fill='x')
    return panel
