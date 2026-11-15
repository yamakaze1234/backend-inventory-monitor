"""监控覆盖视图：列表优先，长说明只在选中后展开。"""
import csv
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from inventory_theme import BG, INK, MUTED, BLUE, RoundedCard, ScrollBar, button, sync_tree, wrap_label

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

