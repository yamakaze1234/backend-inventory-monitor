"""Editable local report preview with explicit, recipient-scoped delivery."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from inventory_theme import ScrollBar

from notification_robots import store_for_service
from inventory_report_delivery import destinations, send_reviewed, split_message


class ReportPreview(tk.Toplevel):
    def __init__(self, parent, service, path):
        super().__init__(parent)
        self.withdraw()
        self.path = Path(path)
        self.busy = False
        self.closed = False
        self.events = queue.Queue()
        self.poll_id = None
        try:
            body = self.path.read_text(encoding='utf-8')
            self.store = store_for_service(service)
            self.targets = destinations(self.store)
        except Exception:
            self.destroy()
            raise
        self.title('货源报告 · 本地预览与发送')
        self.geometry('900x760')
        self.minsize(650, 500)
        self.protocol('WM_DELETE_WINDOW', self.close_preview)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text=f'本地文件：{self.path}', wraplength=850).grid(
            row=0, column=0, sticky='w', padx=16, pady=10)
        editor_frame=tk.Frame(self,bg='white')
        editor_frame.grid(row=1,column=0,sticky='nsew',padx=16)
        self.editor = tk.Text(editor_frame, wrap='word', undo=True, font=('Microsoft YaHei UI', 11),bd=0,highlightthickness=0)
        editor_scroll=ScrollBar(editor_frame,command=self.editor.yview)
        editor_scroll.pack(side='right',fill='y')
        self.editor.pack(side='left',fill='both',expand=True)
        self.editor.configure(yscrollcommand=editor_scroll.set)
        self.editor.insert('1.0', body)
        self.editor.edit_modified(False)
        self.editor.bind('<<Modified>>', self.content_changed)
        target_frame = ttk.LabelFrame(self, text='接收机器人（请手动选择）', padding=10)
        target_frame.grid(row=2, column=0, sticky='ew', padx=16, pady=10)
        self.target_vars = {}
        self.target_checks = {}
        self.reviewed = tk.BooleanVar(self, False)
        for row, target in enumerate(self.targets):
            var = tk.BooleanVar(self, False)
            check = ttk.Checkbutton(target_frame, text=target['name'] + (
                '' if target['enabled'] else '（已暂停）'), variable=var)
            check.grid(row=row, column=0, sticky='w')
            if not target['enabled']:
                check.state(['disabled'])
            self.target_vars[target['id']] = var
            self.target_checks[target['id']] = check
            var.trace_add('write', self.selection_changed)
        if not self.targets:
            ttk.Label(target_frame, text='尚未配置机器人，请先在通知设置中添加。').grid()
        self.review_check = ttk.Checkbutton(self, text='已检查内容和接收对象', variable=self.reviewed)
        self.review_check.grid(row=3, column=0, sticky='w', padx=16)
        self.reviewed.trace_add('write', lambda *_: self.refresh_send())
        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, sticky='ew', padx=16, pady=10)
        self.save_button = ttk.Button(buttons, text='保存预览到本地', command=self.save_local)
        self.save_button.pack(side='left')
        self.send_button = ttk.Button(buttons, text='确认发送', command=self.confirm_send)
        self.send_button.pack(side='right')
        self.result_var = tk.StringVar(self, '预览不会自动发送。')
        ttk.Label(self, textvariable=self.result_var, wraplength=850, justify='left').grid(
            row=5, column=0, sticky='ew', padx=16, pady=(0, 16))
        self.refresh_send()
        self.deiconify()

    def body(self):
        return self.editor.get('1.0', 'end-1c')

    def selected(self):
        return [dict(t) for t in self.targets if t['enabled'] and self.target_vars[t['id']].get()]

    def selection_changed(self, *_):
        self.reviewed.set(False)
        self.refresh_send()

    def content_changed(self, _event=None):
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self.reviewed.set(False)
            self.refresh_send()

    def refresh_send(self):
        if hasattr(self, 'send_button'):
            allowed = not self.busy and self.reviewed.get() and self.selected() and self.body().strip()
            self.send_button.state(['!disabled'] if allowed else ['disabled'])

    def save_local(self):
        if self.busy:
            return
        try:
            self.path.write_text(self.body(), encoding='utf-8')
            self.result_var.set('预览已保存到本地。')
        except OSError:
            messagebox.showerror('保存失败', '无法保存本地文件，请检查目录和文件权限。', parent=self)

    def confirm_send(self):
        if self.busy or not self.reviewed.get():
            return
        body, targets = self.body(), self.selected()
        if not targets or not body.strip():
            return
        count = len(split_message(body))
        names = '\n'.join('• ' + t['name'] for t in targets)
        if not messagebox.askyesno('确认发送报告',
                f'接收机器人：\n{names}\n\n每个机器人将收到 {count} 段消息。\n确认发送当前预览内容？', parent=self):
            return
        self.busy = True
        self.editor.configure(state='disabled')
        self.save_button.state(['disabled'])
        self.review_check.state(['disabled'])
        for check in self.target_checks.values():
            check.state(['disabled'])
        self.refresh_send()
        self.result_var.set('正在发送，请等待每个机器人的结果。')
        # Worker only uses immutable snapshots and a queue; Tk stays on the main thread.
        store, path, events = self.store, self.path, self.events
        def worker():
            try:
                results = send_reviewed(store, path, body, targets)
                events.put(('result', results))
            except ValueError as error:
                events.put(('validation', str(error)))
            except Exception:
                # Exception text may contain transport secrets; never render it.
                events.put(('error', [dict(t, status='unknown', sent_parts=0, total_parts=count)
                                      for t in targets]))
        try:
            threading.Thread(target=worker, name='report-preview-delivery', daemon=True).start()
        except RuntimeError:
            self.events.put(('validation', '无法启动发送任务，请稍后再试。'))
            self.poll_results()
            return
        self.poll_id = self.after(100, self.poll_results)

    def poll_results(self):
        self.poll_id = None
        if self.closed:
            return
        try:
            kind, results = self.events.get_nowait()
        except queue.Empty:
            self.poll_id = self.after(100, self.poll_results)
            return
        self.busy = False
        self.editor.configure(state='normal')
        self.save_button.state(['!disabled'])
        self.review_check.state(['!disabled'])
        for t in self.targets:
            if t['enabled']:
                self.target_checks[t['id']].state(['!disabled'])
        self.reviewed.set(False)
        if kind == 'validation':
            self.result_var.set('未发送：' + results + '\n请关闭并重新打开预览后检查接收对象。')
            self.refresh_send()
            return
        labels = {'sent': '已发送', 'partial': '部分发送', 'failed': '失败', 'unknown': '结果待核实'}
        lines = [f"{r['name']}：{labels.get(r['status'], '结果待核实')}（{r.get('sent_parts', 0)}/{r.get('total_parts', 0)} 段）"
                 for r in results]
        if kind == 'error':
            lines.insert(0, '发送未正常结束，结果待核实。')
        lines.append('本次操作不自动重试；结果待核实或部分发送时，请先核实群内消息。')
        self.result_var.set('\n'.join(lines))
        self.refresh_send()

    def close_preview(self):
        if self.busy:
            messagebox.showinfo('正在发送', '发送尚未结束，请等待结果后关闭预览。', parent=self)
            return
        self.closed = True
        if self.poll_id is not None:
            self.after_cancel(self.poll_id)
            self.poll_id = None
        self.destroy()


def open_preview(parent, service, path):
    return ReportPreview(parent, service, path)
