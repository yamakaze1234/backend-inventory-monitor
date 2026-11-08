"""Source selection within the existing collection settings page."""
import tkinter as tk
from tkinter import ttk
from inventory_theme import section, button, INK, MUTED, wrap_label


class SourceSettings(tk.Frame):
    def __init__(self, parent, service, on_change):
        super().__init__(parent,bg='white')
        self.service, self.on_change = service, on_change
        with service.db() as db:
            saved = service.get(db,'sql_connection',{})
            remember = service.get(db,'sql_remember',False)
        self.sql_form = tk.Frame(self,bg='white')
        self.fields = {}
        defaults = dict(server='',port='1433',database='ERP',user='',password='')
        for i,(key,label) in enumerate([('server','服务器'),('port','端口'),('database','数据库'),('user','查询账号'),('password','密码')]):
            tk.Label(self.sql_form,text=label,bg='white',fg=INK,anchor='w').grid(row=i,column=0,sticky='w',padx=(0,20),pady=5)
            self.fields[key] = tk.StringVar(self,str(saved.get(key,defaults[key])) if key!='password' else '')
            ttk.Entry(self.sql_form,textvariable=self.fields[key],width=36,show='●' if key=='password' else '').grid(row=i,column=1,sticky='ew',pady=5)
        self.remember = tk.BooleanVar(self,remember)
        ttk.Checkbutton(self.sql_form,text='记住数据库密码（当前 Windows 用户加密保存，重启自动连接）',variable=self.remember).grid(row=5,column=0,columnspan=2,sticky='w',pady=8)
        self.sql_form.columnconfigure(1,weight=1)
        self.sql_form.pack(fill='x')
        actions=tk.Frame(self,bg='white'); actions.pack(fill='x',pady=(10,0))
        self.actions=actions
        button(actions,'应用来源 / 连接',self.apply,primary=True).pack(side='left')
        self.refresh_button=button(actions,'刷新完整商品目录',self.refresh_catalog)
        self.refresh_button.pack(side='left',padx=10)
        note=tk.Label(self,text='SQL 无需打开 ERP 网页。缺少分库待入时额外监测 ERP 总待入，并标注“仓库未确认”。切换来源首轮建立基线。',bg='white',fg=MUTED,anchor='w',justify='left')
        note.pack(fill='x',pady=(12,0));wrap_label(note)
        self.message=tk.StringVar(self)
        tk.Label(self,textvariable=self.message,bg='white',fg=INK,wraplength=820,justify='left',anchor='w').pack(fill='x',pady=(8,0))

    def apply(self):
        try:
            # 旧版兼容界面也统一使用 SQL 连接。
            values={k:v.get().strip() for k,v in self.fields.items() if k!='password'}
            values['port']=int(values['port'])
            self.service.configure_sql(values,self.fields['password'].get(),self.remember.get())
            self.fields['password'].set('')
            self.message.set('SQL 查询已启用，正在安排读取完整目录。')
            self.on_change()
        except (ValueError,OSError):
            self.message.set('来源未应用：请核对连接信息和密码，或等待当前查询结束后重试。')

    def refresh_catalog(self):
        try:
            self.service.sql_worker.refresh_catalog()
            self.message.set('已安排刷新完整商品目录。')
        except ValueError as error:
            self.message.set(str(error))


def build_section(parent, service, on_change):
    card=section(parent,'库存数据来源','使用 SQL 查询 ERP 库存。')
    panel=SourceSettings(card,service,on_change);panel.pack(fill='x')
    return panel
