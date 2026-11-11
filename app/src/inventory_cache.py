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
import threading

_KEY=secrets.token_bytes(32)
_TABLES={'messages':'群聊消息缓存','events':'已完成历史提醒'}
_TERMINAL=('sent','cancelled','skipped')
SCOPES={'messages':'群聊消息缓存','events':'已完成历史提醒','reports':'货源周报中间缓存','logs':'诊断日志','exports':'抖店历史导出'}
_LOCKS={}
_LOCKS_GUARD=threading.Lock()


def cleanup_lock(root):
    # 手动、定时与旧导出清理共享锁，不能同时处理同一份归档。
    key=str(Path(root).resolve()).casefold()
    with _LOCKS_GUARD:return _LOCKS.setdefault(key,threading.RLock())


def normalize_scope(scopes):
    if scopes is None:return list(SCOPES)
    if not isinstance(scopes,list) or not scopes or any(not isinstance(s,str) or s not in SCOPES for s in scopes):
        raise ValueError('请至少选择一种有效的缓存范围')
    return sorted(set(scopes))


def _seal(plan):
    return hmac.new(_KEY,json.dumps({k:v for k,v in plan.items() if k!='seal'},sort_keys=True,ensure_ascii=False).encode(),hashlib.sha256).hexdigest()


def _roots(service):
    with service.db() as db:
        source=service.get(db,'daily_source_root',str(service.root))
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


def preview(service,days=30,now=None,scopes=None):
    if type(days) is not int or not 0<=days<=3650:raise ValueError('保留天数需为 0 至 3650 的整数；0 表示全部可清理缓存')
    scopes=normalize_scope(scopes)
    now=time.time() if now is None else float(now)
    cutoff=now if days==0 else now-days*86400
    plan=dict(days=days,scopes=scopes,created_at=now,cutoff=cutoff,roots=_roots(service),databases=[],groups=[],files=[],row_count=0,file_count=0,total_bytes=0)
    for root_name in plan['roots']:
        root=Path(root_name);path=root/'inventory.sqlite3'
        if _plain_file(path,root):
            candidates=[]
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table,label in _TABLES.items():
                    if table not in tables or table not in scopes:continue
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
        files=list((root/'weekly_reports').glob('货源周报_*.json')) if 'reports' in scopes else []
        if 'logs' in scopes:files.append(root/'monitor_runtime'/'supervisor_errors.log')
        file_groups={}
        for path in files:
            if path.name.endswith('.delivery.json') or not _plain_file(path,root):continue
            stat=path.stat()
            if stat.st_mtime>=cutoff:continue
            plan['files'].append(dict(path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns,digest=_fingerprint(path)))
            plan['file_count']+=1;plan['total_bytes']+=stat.st_size
            scope='logs' if path.name=='supervisor_errors.log' else 'reports'
            group=file_groups.setdefault(scope,dict(label=root.name+' / '+SCOPES[scope],rows=0,files=0,bytes=0))
            group['files']+=1;group['bytes']+=stat.st_size
        plan['groups'].extend(file_groups.values())
    # 下载表格按归档证据逐项纳入预览；不扫描浏览器登录目录或任意下载文件。
    from douyin_export_cleanup import historical_export_files
    seen = {item['path'] for item in plan['files']}
    for root_name in plan['roots']:
        paths = historical_export_files(root_name, cutoff) if 'exports' in scopes else []
        count = size = 0
        for path in paths:
            if str(path) in seen:
                continue
            stat = path.stat()
            plan['files'].append(dict(path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns,
                                      digest=_fingerprint(path),export_root=root_name))
            seen.add(str(path)); count += 1; size += stat.st_size
        if count:
            plan['groups'].append(dict(label='抖店历史下载表格及归档凭据', rows=0, bytes=size, files=count))
            plan['file_count'] += count; plan['total_bytes'] += size
    plan['seal']=_seal(plan)
    return plan


def recycle_file(path):
    """Windows recycle-bin API only; no permanent-delete fallback."""
    from inventory_recycle import recycle_file as native_recycle
    native_recycle(path)


def execute(service,plan,recycler=None,now=None):
    # 数据库行先回收可恢复副本再删除，文件只使用回收站，不永久删除。
    with cleanup_lock(service.root):return _execute(service,plan,recycler=recycler,now=now)


def _execute(service,plan,recycler=None,now=None):
    if not isinstance(plan,dict) or not hmac.compare_digest(str(plan.get('seal','')),_seal(plan)):
        raise ValueError('清理清单已失效，请重新预览')
    now=time.time() if now is None else float(now)
    if not 0<=now-plan.get('created_at',0)<=1800:raise ValueError('清理预览已超过30分钟，请重新预览')
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
    # 执行前重新计算保护范围，预览之后成为当前表的文件不得回收。
    from douyin_export_cleanup import historical_export_groups
    export_allowed = {};evidence_dependencies={}
    for owner in {item.get('export_root') for item in plan['files'] if item.get('export_root')}:
        if owner in plan['roots']:
            groups=historical_export_groups(owner,plan['cutoff'])
            export_allowed[owner]={p for group in groups for p in group.values() if p is not None}
            for group in groups:
                if group['evidence'] is not None:
                    evidence_dependencies[group['evidence']]={group[k] for k in ('archive','raw') if group[k] is not None}
    recycled=set()
    # 不依赖预览列表排序；证据必须最后回收，任一依赖未成功则保留它以便重试。
    files=sorted(plan['files'],key=lambda item:Path(item['path']).resolve() in evidence_dependencies)
    for candidate in files:
        path=Path(candidate['path'])
        try:
            owner = candidate.get('export_root')
            allowed = path.resolve() in export_allowed.get(owner, set()) if owner else any(_plain_file(path,Path(root)) for root in plan['roots'])
            if not allowed:
                result['skipped']+=1;continue
            if path.resolve() in evidence_dependencies and not evidence_dependencies[path.resolve()]<=recycled:
                result['skipped']+=1;continue
            stat=path.stat()
            if (stat.st_size!=candidate['size'] or stat.st_mtime_ns!=candidate['mtime_ns'] or _fingerprint(path)!=candidate['digest']):
                result['skipped']+=1;continue
            recycler(path)
            if path.exists():raise OSError('未回收')
            result['recycled_files']+=1
            recycled.add(path.resolve())
        except Exception:
            result['errors'].append(path.name+'：未能移入回收站，未强制删除。')
    return result
