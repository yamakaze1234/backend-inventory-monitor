"""定时订单与发货通知复用工作台勾选及逐群回执，人工指令保持原群回复。"""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess

from patrol_time_window import department_workspace


def configured_store(channel):
    workspace=department_workspace()
    if workspace is None:
        return None
    root=workspace
    inventory=Path(workspace)/'inventory.sqlite3'
    if inventory.is_file():
        with closing(sqlite3.connect(inventory,timeout=5)) as db:
            row=db.execute("SELECT data FROM meta WHERE key='daily_source_root'").fetchone()
            if row:
                root=Path(json.loads(row[0]))
    path=Path(root)/'notification_robots.sqlite3'
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path,timeout=5)) as db:
        managed=db.execute('SELECT 1 FROM migrations WHERE id=?',('managed:'+channel,)).fetchone()
    if not managed:
        return None  # 尚未迁移的用户继续使用其原先的单群设置。
    from notification_robots import RobotStore
    return RobotStore(root)


def audience(channel):
    store=configured_store(channel)
    if store is None:
        return None
    with store.db() as db:
        # 通知范围或目标改变后重新计算业务去重键，不记录明文凭据。
        return [tuple(row) for row in db.execute('SELECT id,enabled,channels,since,fingerprint,deleted FROM robots ORDER BY id')
                if channel in json.loads(row[2])]


def deliver(db,job,channel,text,dws,runner,delivery_key=None):
    store=configured_store(channel)
    if store is None:
        return False
    if db.execute("UPDATE jobs SET delivery='unknown' WHERE id=? AND delivery='pending'",(job['id'],)).rowcount!=1:
        return True

    def enterprise(target,payload):
        # 身份与群来自已校验的通知设置；不回退到接单群或个人身份。
        from notification_enterprise import validate_target
        target=validate_target(target)
        result=runner([dws,'chat','+messages-send','--as','bot','--robot-code',target['robot_code'],
                       '--groups',target['group_id'],'--markdown',payload['markdown']['text'],
                       '--profile',target['profile'],'--format','json','--yes'],
                      capture_output=True,text=True,encoding='utf-8',timeout=45,
                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        value=json.loads(result.stdout)
        return dict(status='sent' if result.returncode==0 and value.get('succeededCount')==1 and value.get('failedCount')==0 else 'unknown')

    receipts=[]
    ids=None
    try:
        for index in range(0,len(text),1500):
            result=store.send(channel,(delivery_key or job['id'])+':'+str(index//1500),
                dict(msgtype='markdown',markdown=dict(title='发货监控提醒' if channel=='shipping' else '订单巡检结果',text=text[index:index+1500])),
                job['created'],recipient_ids=ids,retry_failed=False,enterprise_transport=enterprise)
            if ids is None:
                ids=[r['id'] for r in result['recipients']]
            receipts.append(result)
            db.execute('UPDATE jobs SET evidence=? WHERE id=?',(json.dumps(receipts,ensure_ascii=False),job['id']))
        statuses={r['status'] for r in receipts if r['status']!='skipped'}
        state='sent' if statuses=={'sent'} else 'suppressed' if not statuses else 'partial' if 'sent' in statuses or 'partial' in statuses else 'failed' if statuses=={'failed'} else 'unknown'
        db.execute('UPDATE jobs SET delivery=? WHERE id=?',(state,job['id']))
    except Exception:
        # 发送已开始的异常保留未知；已有逐群回执不会清空或重发。
        db.execute('UPDATE jobs SET evidence=? WHERE id=?',(json.dumps(dict(message='通知投递未完全确认，请检查逐群回执',receipts=receipts),ensure_ascii=False),job['id']))
    return True
