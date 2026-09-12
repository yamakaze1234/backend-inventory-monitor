"""Allowlisted local cache cleanup. Records are recycled before deletion."""
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

_KEY=secrets.token_bytes(32)
_TABLES={'messages':'群聊消息缓存','events':'已完成历史提醒'}
_TERMINAL=('sent','cancelled','skipped')


def _seal(plan):
    return hmac.new(_KEY,json.dumps({k:v for k,v in plan.items() if k!='seal'},sort_keys=True,ensure_ascii=False).encode(),hashlib.sha256).hexdigest()


def _roots(service):
    from monitor_portable_install import default_data_dir
    with service.db() as db:
        source=service.get(db,'daily_source_root',str(default_data_dir()))
    return sorted({str(service.root.resolve()),str(Path(source).resolve())})


def _plain_file(path,root):
    return (path.is_file() and not path.is_symlink() and
            path.resolve().is_relative_to(root) and
            not any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction())
                    for p in [path.parent] if p!=root))


def _fingerprint(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def preview(service,days=30,now=None):
    if type(days) is not int or days not in (0,7,30,90):raise ValueError('请选择7、30、90天前或全部')
    now=time.time() if now is None else float(now)
    cutoff=now if days==0 else now-days*86400
    plan=dict(days=days,cutoff=cutoff,roots=_roots(service),databases=[],groups=[],files=[],row_count=0,file_count=0,total_bytes=0)
    for root_name in plan['roots']:
        root=Path(root_name);path=root/'inventory.sqlite3'
        if _plain_file(path,root):
            candidates=[]
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table,label in _TABLES.items():
                    if table not in tables:continue
                    query=f'SELECT * FROM {table} WHERE created < ?'
                    parameters=[cutoff]
                    if table=='events':
                        query+=' AND status IN (?,?,?)';parameters.extend(_TERMINAL)
                    cursor=db.execute(query,parameters)
                    columns=[d[0] for d in cursor.description]
                    count=size=0
                    for values in cursor:
                        encoded=json.dumps(dict(zip(columns,values)),ensure_ascii=False,sort_keys=True).encode()
                        candidates.append(dict(table=table,id=values[0],digest=hashlib.sha256(encoded).hexdigest()))
                        count+=1;size+=len(encoded)
                    if count:plan['groups'].append(dict(label=root.name+' / '+label,rows=count,bytes=size))
                    plan['row_count']+=count;plan['total_bytes']+=size
            if candidates:plan['databases'].append(dict(path=str(path),records=candidates))
        files=list((root/'weekly_reports').glob('货源周报_*.json'))
        files.append(root/'monitor_runtime'/'supervisor_errors.log')
        for path in files:
            if path.name.endswith('.delivery.json') or not _plain_file(path,root):continue
            stat=path.stat()
            if stat.st_mtime>=cutoff:continue
            plan['files'].append(dict(path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns,digest=_fingerprint(path)))
            plan['file_count']+=1;plan['total_bytes']+=stat.st_size
    plan['seal']=_seal(plan)
    return plan


def recycle_file(path):
    """Windows recycle-bin API only; no permanent-delete fallback."""
    from inventory_recycle import recycle_file as native_recycle
    native_recycle(path)


def execute(service,plan,recycler=None):
    if not isinstance(plan,dict) or not hmac.compare_digest(str(plan.get('seal','')),_seal(plan)):
        raise ValueError('清理清单已失效，请重新预览')
    if plan['roots']!=_roots(service):raise ValueError('数据目录已变化，请重新预览')
    recycler=recycler or recycle_file
    result=dict(deleted_rows=0,recycled_files=0,skipped=0,errors=[])
    for entry in plan['databases']:
        path=Path(entry['path'])
        if not _plain_file(path,path.parent):
            result['skipped']+=len(entry['records']);continue
        archive=None
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=15)) as db:
                db.execute('PRAGMA secure_delete=ON')
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    records=[]
                    for candidate in entry['records']:
                        table=candidate['table']
                        cursor=db.execute(f'SELECT * FROM {table} WHERE id=?',(candidate['id'],))
                        values=cursor.fetchone()
                        if values is None:result['skipped']+=1;continue
                        row=dict(zip([d[0] for d in cursor.description],values))
                        digest=hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                        if digest!=candidate['digest']:
                            result['skipped']+=1;continue
                        records.append(dict(table=table,row=row))
                    if not records:continue
                    archive_dir=service.root.resolve()/'cache_cleanup_backups'
                    if archive_dir.is_symlink() or (hasattr(archive_dir,'is_junction') and archive_dir.is_junction()):
                        raise OSError('备份路径异常')
                    archive_dir.mkdir(exist_ok=True)
                    archive=archive_dir/('cache-records-'+uuid.uuid4().hex+'.json')
                    archive.write_text(json.dumps(dict(database=str(path),created_at=time.time(),records=records),ensure_ascii=False,indent=2),encoding='utf-8')
                    recycler(archive)
                    if archive.exists():raise OSError('备份未进入回收站')
                    for record in records:
                        db.execute(f"DELETE FROM {record['table']} WHERE id=?",(record['row']['id'],))
                    if any(r['table']=='messages' for r in records):
                        db.execute("DELETE FROM meta WHERE key='history_coverage'")
                result['deleted_rows']+=len(records)
        except Exception:
            result['errors'].append(path.parent.name+'：数据库缓存未完成清理；未能回收备份或数据库忙，请重试。')
    for candidate in plan['files']:
        path=Path(candidate['path'])
        try:
            if not any(_plain_file(path,Path(root)) for root in plan['roots']):
                result['skipped']+=1;continue
            stat=path.stat()
            if (stat.st_size!=candidate['size'] or stat.st_mtime_ns!=candidate['mtime_ns'] or _fingerprint(path)!=candidate['digest']):
                result['skipped']+=1;continue
            recycler(path)
            if path.exists():raise OSError('未回收')
            result['recycled_files']+=1
        except Exception:
            result['errors'].append(path.name+'：未能移入回收站，未强制删除。')
    return result
