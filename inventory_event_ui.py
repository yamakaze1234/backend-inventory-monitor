"""Read-only reminder history; never creates or retries a delivery."""
import time
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter.scrolledtext import ScrolledText
from inventory_theme import BG, INK, button
from inventory_delivery import format_events, format_inventory_report

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
        self.text=ScrolledText(self,wrap='word',font=('Microsoft YaHei UI',10),padx=14,pady=12)
        self.text.pack(fill='both',expand=True)
        actions=tk.Frame(self,bg=BG);actions.pack(fill='x',pady=(10,0))
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
            content=(history_summary(events)+'\n\n'+format_events(events,self.service)).strip() if events else '当前筛选条件下暂无提醒。首次检查建立库存基线。'
            self.text.configure(state='normal');self.text.delete('1.0','end');self.text.insert('1.0',content);self.text.configure(state='disabled')
            self.signature=signature
        self.timer=self.after(1500,self.refresh)

    def export(self):
        self.refresh();self.save(self.text.get('1.0','end'),'库存提醒记录.md')

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
