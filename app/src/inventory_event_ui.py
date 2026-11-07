"""Read-only reminder history; never creates or retries a delivery."""
import time
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter.scrolledtext import ScrolledText
from inventory_theme import BG, INK, button, ScrollBar
from inventory_delivery import format_events, format_events_detailed, format_inventory_report
from inventory_message_view import LABELS, event_change
from inventory_insight_ui import table
from inventory_theme import sync_tree

KINDS={'全部类型':None,'有货待到':{'pending_inbound'},'库存增加':{'stock_increase','inbound'},
       '库存预警':{'low','out_of_stock','oversold','negative_stock'},'风险恢复':{'recovery'}}

def history_summary(events):
    lines=['# 变化摘要']
    states={'pending':'待发送','sent':'已发送','unknown':'送达待核实','failed':'发送失败','partial':'部分送达','cancelled':'已取消','skipped':'已跳过','sending':'发送中'}
    for event in events:
        current,previous=event.get('after') or {},event.get('before') or {}
        special=current.get('stock_basis')=='warehouse_stock'
        stock='warehouse_stock' if special else 'stock'
        pending='warehouse_purchase' if special else 'purchase'
        text=''
        if event['kind']=='stock_increase' and current.get(stock) is not None and previous.get(stock) is not None:
            text=f"库存增加 {current[stock]-previous[stock]:g} 件：{previous[stock]:g} → {current[stock]:g}"
        elif event['kind']=='pending_inbound' and current.get(pending) is not None:
            text=f"当前有 {current[pending]:g} 件待到货"
            text+=f"，待入增加 {current[pending]-previous[pending]:g} 件" if previous.get(pending) is not None else '（首次取得待入数量）'
        if text:
            lines.append(f"- {event.get('name','')} · {event.get('warehouse') or '公司大库'}：{text} · {states.get(event.get('delivery_status'),'待核实')}")
    return '\n'.join(lines) if len(lines)>1 else ''

class EventPanel(tk.Frame):
    def __init__(self,parent,service):
        super().__init__(parent,bg=BG,padx=16,pady=12)
        self.service=service;self.signature=None;self.timer=None
        self.event_rows = {}
        self.view = tk.StringVar(self, '所选详情')
        row=tk.Frame(self,bg=BG);row.pack(fill='x')
        self.kind=tk.StringVar(self,'全部类型');self.warehouse=tk.StringVar(self,'全部仓库')
        self.period=tk.StringVar(self,'全部时间');self.query=tk.StringVar(self)
        for label,var,values in [('类型',self.kind,tuple(KINDS)),('仓库',self.warehouse,('全部仓库',)),('时间',self.period,('全部时间','今天','近24小时','近7天'))]:
            tk.Label(row,text=label,bg=BG).pack(side='left',padx=(0,6))
            combo=ttk.Combobox(row,textvariable=var,values=values,state='readonly',width=12)
            combo.pack(side='left',padx=(0,14))
            if label=='仓库':self.warehouse_combo=combo
        find=tk.Frame(self,bg=BG);find.pack(fill='x',pady=10)
        tk.Label(find,text='商品 / ERP 编号',bg=BG).pack(side='left')
        ttk.Entry(find,textvariable=self.query,width=32).pack(side='left',padx=10,fill='x',expand=True)
        self.summary=tk.StringVar(self)
        tk.Label(self,textvariable=self.summary,bg=BG,fg=INK,anchor='w').pack(fill='x',pady=(0,10))
        # 列表展示所有事件，详情只展示选中项，避免重复铺满完整通知。
        self.tree = table(self, dict(time='采集时间', kind='事件', name='商品', change='数量变化', warehouse='仓库', delivery='投递状态'), (115, 85, 260, 180, 85, 85), height=8)
        self.tree.bind('<<TreeviewSelect>>', self.show_selected)
        mode = tk.Frame(self, bg=BG)
        mode.pack(side='bottom', fill='x', pady=(8, 5), before=self.tree.master)
        ttk.Radiobutton(mode, text='所选事件详情', variable=self.view, value='所选详情', command=self.show_selected).pack(side='left')
        ttk.Radiobutton(mode, text='当前筛选消息预览', variable=self.view, value='消息预览', command=self.show_selected).pack(side='left', padx=12)
        # 消息详情复用应用滚动条和无边框文本区，避免旧式系统边框混入工作区。
        detail=tk.Frame(self,bg='white')
        detail.pack(side='bottom', fill='x', before=mode)
        self.text=tk.Text(detail,wrap='word',font=('Microsoft YaHei UI',10),height=5,padx=14,pady=10,
                          bg='white',fg=INK,relief='flat',borderwidth=0)
        scroll=ScrollBar(detail,command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.text.pack(fill='x',expand=True)
        actions=tk.Frame(self,bg=BG);actions.pack(side='bottom',fill='x',pady=(8,0),before=detail)
        button(actions,'导出当前筛选提醒',self.export).pack(side='left')
        button(actions,'导出当前库存快照',self.export_inventory).pack(side='left',padx=10)
        self.refresh()

    def refresh(self):
        if self.timer:self.after_cancel(self.timer)
        status=self.service.status();now=time.time()
        names=set(status.get('known_warehouses',[]))
        names.update(e.get('warehouse') or '公司大库' for e in status['events'])
        names.update(p.get('warehouse_name') or '公司大库' for p in status['products'])
        self.warehouse_combo['values']=['全部仓库']+sorted(names)
        kind,warehouse,period,query=(v.get() for v in (self.kind,self.warehouse,self.period,self.query))
        signature=(tuple((e['id'],e['delivery_status']) for e in status['events']),kind,warehouse,period,query,int(now//60))
        if signature!=self.signature:
            since=0
            if period=='今天':
                local=time.localtime(now);since=time.mktime((local.tm_year,local.tm_mon,local.tm_mday,0,0,0,0,0,-1))
            elif period in ('近24小时','近7天'):since=now-{'近24小时':86400,'近7天':604800}[period]
            events,total=self.service.preview_events(kinds=KINDS[kind],warehouse='' if warehouse=='全部仓库' else warehouse,query=query,since=since)
            self.summary.set(f'匹配 {total} 条 · 显示最近 {len(events)} 条（最多 200 条）· 查看和导出不会重新发送')
            self.event_rows = {e['id']: e for e in events}
            states = {'sent': '已发送', 'pending': '待发送', 'unknown': '待核实', 'failed': '失败待重试', 'partial': '部分送达', 'cancelled': '已取消', 'preview': '本地预览', 'sending': '发送中', 'skipped': '已跳过'}
            sync_tree(self.tree, [(e['id'], (time.strftime('%m-%d %H:%M:%S', time.localtime(e.get('observed_at', 0))),
                LABELS.get(e['kind'], e['kind']), e['name'], event_change(e), e.get('warehouse') or '公司大库', states.get(e.get('delivery_status'), '待核实')),
                ('warning',) if e['kind'] in ('low','oversold','negative_stock','out_of_stock') else ()) for e in events])
            self.show_selected()
            self.signature=signature
        self.timer=self.after(1500,self.refresh)

    def export(self):
        self.refresh()
        self.save(format_events_detailed(list(self.event_rows.values()), self.service),'库存提醒记录.md')

    def show_selected(self, *_):
        if self.view.get() == '消息预览':
            events = list(self.event_rows.values())
            # 离线排版预览，不构造发送器；一屏只预览前 6 项。
            content = format_events(events[:6], self.service)
            if len(events) > 6:
                content += f'\n\n预览前 6 项，共 {len(events)} 项；可在上方列表继续查看。'
        else:
            selected = next(iter(self.tree.selection()), '')
            event = self.event_rows.get(selected)
            content = format_events_detailed([event], self.service) if event else '选择上方一条事件查看完整库存、入库单据和货源消息。'
        if getattr(self, '_display_content', None) != content:
            self._display_content = content
            render_message(self.text, content)

    def export_inventory(self):
        status=self.service.status()
        self.save(format_inventory_report(status,refresh_pending=status.get('source_busy',False) or status['connection']!='online'),'库存检查快照.md')

    def save(self,content,name):
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.md',initialfile=name)
        if path:
            from pathlib import Path
            Path(path).write_text(content,encoding='utf-8')

    def destroy(self):
        if self.timer:self.after_cancel(self.timer)
        super().destroy()


def render_message(widget, content):
    """在原生视图呈现标题和加粗内容，避免把 Markdown 标记直接展示给运营。"""
    import re
    widget.configure(state='normal')
    widget.delete('1.0', 'end')
    widget.tag_configure('heading', font=('Microsoft YaHei UI', 11, 'bold'), spacing1=4, spacing3=4)
    widget.tag_configure('strong', font=('Microsoft YaHei UI', 10, 'bold'))
    widget.tag_configure('normal', spacing3=2)
    for line in content.splitlines():
        line = line.rstrip()
        if line.startswith('#'):
            widget.insert('end', re.sub(r'^#+\s*', '', line) + '\n', 'heading')
        elif line == '---':
            widget.insert('end', '\n')
        else:
            for index, segment in enumerate(line.split('**')):
                widget.insert('end', segment, 'strong' if index % 2 else 'normal')
            widget.insert('end', '\n')
    widget.configure(state='disabled')
