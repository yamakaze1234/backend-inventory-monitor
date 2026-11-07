"""店铺级低库存设置窗口：默认阈值与精确配件例外。"""
import tkinter as tk
from tkinter import ttk
from inventory_theme import BG, INK, MUTED, button


def open_rules(owner):
    ident = owner.ident
    if not ident:
        owner.notice.set('请先选择店铺')
        return
    store = owner.runtime.store
    rules = store.low_stock_rules(ident)
    names = {str(p['goods_id']): p.get('name', '') for row in store.snapshots(ident)
             for p in row.get('erp', {}).get('parts', []) if p.get('goods_id')}
    win = tk.Toplevel(owner)
    win.title('低库存预警 · ' + store.store(ident)['name'])
    win.geometry('760x520')
    win.minsize(680, 460)
    win.configure(bg=BG)
    box = tk.Frame(win, bg=BG, padx=18, pady=16)
    box.pack(fill='both', expand=True)
    default = tk.StringVar(win, str(rules['default']))
    gid = tk.StringVar(win)
    limit = tk.StringVar(win, '1')
    muted = tk.BooleanVar(win, False)
    notice = tk.StringVar(win)
    top = tk.Frame(box, bg=BG); top.pack(fill='x')
    tk.Label(top, text='抖店 SKU 库存 ≤', bg=BG, fg=INK).pack(side='left')
    ttk.Spinbox(top, textvariable=default, from_=0, to=1000000, width=7).pack(side='left', padx=8)
    tk.Label(top, text='时预警（仅当前店铺）', bg=BG, fg=INK).pack(side='left')
    tk.Label(box, text='包含下列配件的配置使用单独规则。不预警优先；其余例外取较高阈值。\n配件以 ERP 编号匹配；SKU 未匹配记录仍保留人工核对提醒。',
             bg=BG, fg=MUTED, justify='left', anchor='w').pack(fill='x', pady=12)
    edit = tk.Frame(box, bg=BG); edit.pack(fill='x')
    choices = [key + ' · ' + name for key, name in sorted(names.items())]
    entry = ttk.Combobox(edit, textvariable=gid, values=choices, width=34)
    entry.pack(side='left', fill='x', expand=True)
    ttk.Spinbox(edit, textvariable=limit, from_=0, to=1000000, width=6).pack(side='left', padx=8)
    ttk.Checkbutton(edit, text='不预警', variable=muted).pack(side='left')
    table = ttk.Treeview(box, columns=('name', 'rule'), show='tree headings', height=9)
    table.heading('#0', text='ERP 编号'); table.column('#0', width=105, stretch=False)
    table.heading('name', text='配件'); table.column('name', width=350)
    table.heading('rule', text='预警规则'); table.column('rule', width=110, stretch=False)
    table.pack(fill='both', expand=True, pady=12)
    def render():
        table.delete(*table.get_children())
        for key, value in rules['parts'].items():
            table.insert('', 'end', iid=key, text=key, values=(names.get(key,'手工指定 ERP 编号'),
                '不预警' if value is None else '库存 ≤ ' + str(value)))
    def add():
        from douyin_low_stock import validate
        try:
            key = gid.get().split(' · ')[0].strip()
            value = None if muted.get() else int(limit.get())
            candidate = validate(dict(default=int(default.get()), parts={**rules['parts'], key:value}))
            rules.update(candidate); render(); notice.set('规则已加入，点击保存后生效')
        except (ValueError, TypeError):
            notice.set('请输入有效 ERP 编号和非负整数阈值')
    def remove():
        for key in table.selection():
            rules['parts'].pop(key, None)
        render(); notice.set('已移除例外，保存后恢复默认阈值')
    def select(_):
        if table.selection():
            key = table.selection()[0]; value = rules['parts'][key]
            gid.set(key + ' · ' + names.get(key,'')); muted.set(value is None)
            limit.set(str(value if value is not None else 1))
    table.bind('<<TreeviewSelect>>', select)
    actions = tk.Frame(box, bg=BG); actions.pack(side='bottom', fill='x', before=table)
    button(actions, '添加 / 修改规则', add).pack(side='left')
    button(actions, '移除例外', remove).pack(side='left', padx=8)
    def save():
        try:
            store.save_low_stock_rules(ident, dict(default=int(default.get()), parts=rules['parts']))
            owner._stock_signature = None
            owner.refresh_details()
            owner.runtime.submit('online', ident)
            win.destroy()
        except (ValueError, TypeError) as error:
            notice.set(str(error))
    button(actions, '保存设置', save, primary=True).pack(side='right')
    tk.Label(box, textvariable=notice, bg=BG, fg='#98521A', anchor='w').pack(side='bottom', fill='x', pady=(8,0), before=table)
    render()
    return win
