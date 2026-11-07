"""Manual weekly report controls; worker threads never access Tk widgets."""
import os
from pathlib import Path
from queue import Empty, Queue
import threading
import tkinter as tk
from tkinter import ttk

from inventory_theme import INK, MUTED, button, section


def _generate(service, period, progress):
    from inventory_weekly import generate
    return generate(service, period=period, progress=progress)


def _open_preview(parent,service,path):
    from inventory_report_preview import open_preview
    return open_preview(parent,service,path)


def _work(events, service, period):
    # Keep the worker independent of the widget, including after destruction.
    try:
        path = _generate(service, period, lambda message: events.put(('progress', str(message))))
        events.put(('done', Path(path)))
    except Exception:
        # Exceptions may contain service credentials; never display their raw text.
        events.put(('failed', None))


class WeeklySection(tk.Frame):
    def __init__(self, parent, service):
        super().__init__(parent, bg='white')
        self.service = service
        self.events = Queue()
        self.running = False
        self.closed = False
        self.report_path = None
        self.poll_id = None
        self.period = tk.StringVar(self, '本周')
        self.notice = tk.StringVar(self, '选择周期后生成本地 Markdown 周报。')
        tk.Label(self, text='统计周期（北京时间，周一至周日）', bg='white', fg=INK).pack(anchor='w')
        self.period_picker = ttk.Combobox(self, textvariable=self.period, values=('本周', '上周', '自定义日期'), state='readonly', width=12)
        self.period_picker.pack(anchor='w', pady=(8, 12))
        from inventory_weekly import week_range
        first,last=week_range()
        self.start_date=tk.StringVar(self,first.strftime('%Y-%m-%d'))
        self.end_date=tk.StringVar(self,last.strftime('%Y-%m-%d'))
        self.date_frame=tk.Frame(self,bg='white')
        self.date_entries=[]
        for label,var in [('开始日期',self.start_date),('结束日期',self.end_date)]:
            tk.Label(self.date_frame,text=label,bg='white',fg=INK).pack(side='left',padx=(0,6))
            entry=ttk.Entry(self.date_frame,textvariable=var,width=12)
            entry.pack(side='left',padx=(0,12));self.date_entries.append(entry)
        tk.Label(self.date_frame,text='YYYY-MM-DD，包含首尾日期',bg='white',fg=MUTED).pack(side='left')
        self.period_picker.bind('<<ComboboxSelected>>',self._date_visibility)
        self.generate_button = button(self, '一键生成货源周报', self.start, primary=True)
        self.generate_button.pack(anchor='w')
        self.open_button = button(self, '预览并选择机器人', self.open_report, state='disabled')
        self.open_button.pack(anchor='w', pady=(8, 0))
        status = tk.Label(self, textvariable=self.notice, bg='white', fg=MUTED, justify='left', anchor='w')
        status.pack(fill='x', pady=(12, 0))
        status.bind('<Configure>', lambda event: status.configure(wraplength=max(1, event.width)))
        self.bind('<Destroy>', self._on_destroy, add='+')

    def _date_visibility(self,event=None):
        if self.period.get()=='自定义日期':
            self.date_frame.pack(fill='x',pady=(0,12),before=self.generate_button)
        else: self.date_frame.pack_forget()

    def start(self):
        if self.running or self.closed:
            return
        period=self.period.get()
        if period=='自定义日期':
            period=(self.start_date.get().strip(),self.end_date.get().strip())
            from inventory_weekly import week_range
            try: week_range(period)
            except ValueError as error:
                self.notice.set(str(error));return
        self.running = True
        self.report_path = None
        self.generate_button.configure(state='disabled')
        self.open_button.configure(state='disabled')
        self.period_picker.configure(state='disabled')
        for entry in self.date_entries: entry.configure(state='disabled')
        self.notice.set('正在生成货源周报…')
        try:
            threading.Thread(target=_work, args=(self.events, self.service, period), daemon=True).start()
        except RuntimeError:
            self.events.put(('failed', None))
        self.poll_id = self.after(100, self._poll)

    def _poll(self):
        self.poll_id = None
        if self.closed:
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except Empty:
                break
            if kind == 'progress':
                self.notice.set(value)
            else:
                self.running = False
                self.generate_button.configure(state='normal')
                self.period_picker.configure(state='readonly')
                for entry in self.date_entries: entry.configure(state='normal')
                if kind == 'done':
                    self.report_path = value
                    self.notice.set('已生成：' + str(value))
                    self.open_button.configure(state='normal')
                    self.open_report()
                else:
                    self.notice.set('周报生成失败，请检查来源连接及 AI 设置后重试。')
        if self.running:
            self.poll_id = self.after(100, self._poll)

    def open_report(self):
        if self.closed or self.report_path is None:
            return
        try:
            _open_preview(self,self.service,self.report_path)
        except Exception:
            self.notice.set('预览打开失败，可重试。报告已保存：' + str(self.report_path))

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        self.closed = True
        if self.poll_id is not None:
            self.after_cancel(self.poll_id)
            self.poll_id = None


def build_section(frame, service):
    card = section(frame, '货源周报', '支持本周、上周及自定义日期。生成后先预览，检查内容并选择机器人后手动发送。')
    panel = WeeklySection(card, service)
    panel.pack(fill='x')
    return panel
