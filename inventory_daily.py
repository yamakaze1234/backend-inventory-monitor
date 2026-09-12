"""Local daily robot report; one claimed delivery per Beijing calendar day."""
from contextlib import closing
import json
import re
import time
from datetime import datetime, timezone, timedelta

DEFAULTS = dict(enabled=False, times=['11:00', '16:00'], format='supply')
TZ = timezone(timedelta(hours=8))

def settings(service):
    with service.db() as db:
        saved = service.get(db, 'daily_settings', {})
    return {**DEFAULTS, **{k:v for k,v in saved.items() if k in DEFAULTS}}


def save_settings(service, values):
    raw = values.get('times', [])
    times = re.split(r'[,，;；\s]+', raw.strip()) if isinstance(raw, str) else raw
    if not times or len(times)>12 or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t) for t in times):
        raise ValueError('请输入有效时间，用逗号分隔，例如 11:00, 16:00（最多 12 个）。')
    with service.db() as db:
        service.put(db, 'daily_settings', dict(enabled=bool(values.get('enabled')), times=sorted(set(times)),format='supply'))


def render(service, config, now=None):
    import sqlite3
    from pathlib import Path
    from monitor_portable_install import default_data_dir
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, TZ)
    start = today.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    with service.db() as db:
        source_root = service.get(db, 'daily_source_root', str(default_data_dir()))
    from inventory_history import collect
    collect(source_root, now)
    path = Path(source_root) / 'inventory.sqlite3'
    rows=[]
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
            rows=[json.loads(r[0]) for r in db.execute('SELECT data FROM messages WHERE created>=? AND created<=? ORDER BY created', (start,now))]
    parts=['# 货源情况 | '+today.strftime('%Y-%m-%d'), '截至今日 '+today.strftime('%H:%M')]
    from inventory_ai import summarize
    summary = summarize(service, rows, now)
    if summary is None:
        return None
    parts.append(summary)
    return '\n\n'.join(parts)


def run_once(service, sender, now=None):
    now = time.time() if now is None else now
    config = settings(service)
    local = datetime.fromtimestamp(now, TZ)
    due = [t for t in config['times'] if t <= local.strftime('%H:%M')]
    if not config['enabled'] or not due:
        return
    # On late startup send only the latest due slot, avoiding a backlog burst.
    key = 'daily_supply:' + local.strftime('%Y-%m-%d') + ':' + max(due)
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if service.get(db,key): return
        service.put(db,key,dict(status='sending',at=now))
    delivery_started = False
    try:
        body = render(service,config,now)
        if body is None:
            result = {'status':'skipped', 'reason':'截至发送时暂无可汇总的货源消息，本时段不发送。'}
        elif len(body.encode('utf-8')) > 18000:
            result = {'status':'failed', 'reason':'货源消息过长，未发送，请人工检查汇报内容。'}
        else:
            delivery_started = True
            from inventory_delivery import RobotSender
            if isinstance(sender, RobotSender):
                hour, minute = map(int, max(due).split(':'))
                occurred = local.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
                result = sender.send('daily', key, body, occurred)
                if result['status'] == 'skipped':
                    result['reason'] = '本时段没有启用的接收机器人，已跳过。'
            else:
                result = sender(body)
    except Exception:
        result = {'status':'unknown' if delivery_started else 'failed', 'reason':'货源采集或 AI 整理失败，未发送。' if not delivery_started else '发送结果未知。'}
    record = dict(status=result.get('status','unknown'), at=now, reason=result.get('reason',''), recipients=result.get('recipients', []))
    with service.db() as db:
        service.put(db,key,record)
        service.put(db,'daily_last',record)
    return record

def build_page(parent, service):
    import tkinter as tk
    from tkinter import ttk
    from inventory_theme import BG, INK, MUTED, button, ScrollBar, bind_wheel, section
    outer = tk.Frame(parent,bg=BG)
    canvas = tk.Canvas(outer,bg=BG,highlightthickness=0)
    scroll = ScrollBar(outer,command=canvas.yview,orient='vertical',bg=BG)
    scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
    canvas.configure(yscrollcommand=scroll.set)
    frame = tk.Frame(canvas,bg=BG,padx=2,pady=0)
    window=canvas.create_window((0,0),window=frame,anchor='nw')
    canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
    frame.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
    bind_wheel(canvas)
    config = settings(service)
    from inventory_weekly_ui import build_section
    build_section(frame, service)
    card=section(frame,'货源情况定时汇报','按北京时间汇总当天货源消息，并发送到已配置的目标。')
    enabled=tk.BooleanVar(frame,config['enabled'])
    at=tk.StringVar(frame,', '.join(config['times']))
    for label,var in [('启用货源情况定时汇报',enabled)]:
        ttk.Checkbutton(card,text=label,variable=var).pack(anchor='w',pady=6)
    time_label=tk.Label(card,text='每日发送时段（北京时间，多个时间用逗号分隔）',bg='white',fg=INK,justify='left',anchor='w')
    time_label.pack(fill='x',pady=(20,8))
    time_label.bind('<Configure>',lambda e:time_label.configure(wraplength=max(1,e.width)))
    ttk.Entry(card,textvariable=at,font=('Microsoft YaHei UI',12)).pack(fill='x')
    help_label=tk.Label(card,text='默认 11:00、16:00，可添加或修改多个时段。\n截至发送时没有可汇总货源消息，则跳过该时段。\n汇总当日截至发送时的货源消息，并保留发送人、群和时间。\n成本表重开提醒不进入日报。工具需保持运行。\n每个时段只发送一次；错过多个时段，仅补发最近一档。\n未知投递结果不自动重发；数量和到货时间以来源原文为准。',bg='white',fg=MUTED,justify='left',anchor='w',font=('Microsoft YaHei UI',10))
    help_label.pack(fill='x',pady=20)
    help_label.bind('<Configure>',lambda e:help_label.configure(wraplength=max(1,e.width)))
    notice=tk.StringVar(frame,'')
    with service.db() as db: last=service.get(db,'daily_last',{})
    if last:
        labels={'sent':'已发送','failed':'失败','unknown':'结果未知','sending':'发送中','skipped':'已跳过','partial':'部分机器人发送成功'}
        notice.set('最近日报：'+labels.get(last['status'],last['status'])+' '+datetime.fromtimestamp(last['at'],TZ).strftime('%Y-%m-%d %H:%M:%S'))
    def save():
        try:
            save_settings(service,dict(enabled=enabled.get(),times=at.get().strip()))
            notice.set('已保存；自动日报已'+('启用' if enabled.get() else '关闭'))
        except ValueError as error: notice.set(str(error))
    button(card,text='保存日报设置',command=save,primary=True).pack(anchor='w')
    status=tk.Label(card,textvariable=notice,bg='white',fg=MUTED,justify='left',anchor='w')
    status.pack(fill='x',pady=(16,0))
    status.bind('<Configure>',lambda e:status.configure(wraplength=max(1,e.width)))
    from notification_robot_ui import RobotPanel
    from notification_robots import store_for_service
    robots = section(frame, '接收机器人', '与库存提醒、钉钉货源消息共用机器人列表，可分别选择通知类型。')
    RobotPanel(robots, store=store_for_service(service)).pack(fill='x')
    from inventory_ai import build_settings
    build_settings(frame,service)
    return outer
