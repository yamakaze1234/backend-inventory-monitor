"""Explicit reviewed report delivery, with per-robot durable duplicate prevention."""
import hashlib
import json
from pathlib import Path
import time


def destinations(store):
    with store.db() as db:
        return [dict(id=r[0],name=r[1],enabled=bool(r[2]),
                     revision=hashlib.sha256(json.dumps(list(r),ensure_ascii=False).encode()).hexdigest())
                for r in db.execute('SELECT id,name,enabled,fingerprint,since FROM robots WHERE deleted=0 ORDER BY rowid')]


def split_message(body):
    if not isinstance(body,str) or not body.strip(): raise ValueError('报告内容不能为空')
    chunks=[];current='';size=0
    for char in body:
        count=len(char.encode('utf-8'))
        if size+count>16000:
            chunks.append(current);current='';size=0
        current+=char;size+=count
    if current: chunks.append(current)
    return chunks


def send_reviewed(store, path, body, selected, transport=None):
    from notification_robots import send_encrypted
    chunks=split_message(body)
    if not selected or len({t['id'] for t in selected})!=len(selected):
        raise ValueError('请选择接收机器人')
    current={r['id']:r for r in destinations(store)}
    if any(current.get(t['id'])!=t or not t['enabled'] for t in selected):
        raise ValueError('机器人配置已变化，请关闭并重新打开预览后选择')
    path=Path(path).resolve()
    path.write_text(body,encoding='utf-8')
    job=hashlib.sha256((str(path)+'\0'+body).encode('utf-8')).hexdigest()
    transport=transport or send_encrypted
    results=[]
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS report_receipts (
            job TEXT, robot TEXT, name TEXT, status TEXT, sent_parts INTEGER,
            total_parts INTEGER, updated REAL, PRIMARY KEY(job,robot))''')
    for target in selected:
        ident=target['id']
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT name,status,sent_parts,total_parts FROM report_receipts WHERE job=? AND robot=?',(job,ident)).fetchone()
            if prior:
                results.append(dict(id=ident,name=prior[0],status='unknown' if prior[1]=='sending' else prior[1],sent_parts=prior[2],total_parts=prior[3]))
                continue
            row=db.execute('SELECT id,name,enabled,fingerprint,since,secret,deleted FROM robots WHERE id=?',(ident,)).fetchone()
            revision=hashlib.sha256(json.dumps(list(row[:5]),ensure_ascii=False).encode()).hexdigest() if row else None
            if not row or row[6] or not row[2] or revision!=target['revision']:
                results.append(dict(id=ident,name=target['name'],status='failed',sent_parts=0,total_parts=len(chunks)))
                continue
            db.execute('INSERT INTO report_receipts VALUES (?,?,?,?,?,?,?)',(job,ident,target['name'],'sending',0,len(chunks),time.time()))
        sent=0;status='sent'
        for index,chunk in enumerate(chunks,1):
            try:
                result=transport(row[5],dict(msgtype='markdown',markdown=dict(title=f'货源报告 ({index}/{len(chunks)})',text=chunk)))
                status=result.get('status','unknown')
                if status not in ('sent','failed','unknown'): status='unknown'
            except Exception: status='unknown'
            if status!='sent': break
            sent+=1
            with store.db() as db:
                db.execute('UPDATE report_receipts SET sent_parts=?,updated=? WHERE job=? AND robot=?',(sent,time.time(),job,ident))
        # Preserve unknown even after some confirmed parts, so ambiguity is visible.
        if sent and status=='failed': status='partial'
        with store.db() as db:
            db.execute('UPDATE report_receipts SET status=?,sent_parts=?,updated=? WHERE job=? AND robot=?',(status,sent,time.time(),job,ident))
        results.append(dict(id=ident,name=target['name'],status=status,sent_parts=sent,total_parts=len(chunks)))
    path.with_suffix('.delivery.json').write_text(json.dumps(dict(job=job,at=time.time(),recipients=results),ensure_ascii=False,indent=2),encoding='utf-8')
    return results
