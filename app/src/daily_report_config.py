"""日报公开配置：独立工作区、串行保存、备份和原子替换，不接触 DBX 密钥。"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import uuid

FIELDS = {'version','connectionId','connectionName','database','networkMode','dbxExecutable','dbxData','python'}


def validate(value):
    if not isinstance(value,dict) or set(value)!=FIELDS or value.get('version')!=1:
        raise ValueError('日报配置格式或版本不支持')
    if value['database']!='ERP' or value['networkMode']!='direct':
        raise ValueError('日报仅支持 ERP 数据库及直连模式')
    if not isinstance(value['connectionName'],str) or not value['connectionName'].strip() or len(value['connectionName'])>200:
        raise ValueError('请选择 DBX 连接')
    if not isinstance(value['connectionId'],str) or not value['connectionId'].strip():raise ValueError('请选择稳定连接引用')
    for key in ('dbxExecutable','dbxData','python'):
        if not isinstance(value[key],str) or not Path(value[key]).is_absolute():
            raise ValueError('运行路径必须是绝对路径')
    return dict(value)


def load(workspace):
    path=Path(workspace)/'daily-report.json'
    if not path.exists(): return None
    try: return validate(json.loads(path.read_text(encoding='utf-8')))
    except (ValueError,OSError): raise ValueError('日报配置损坏或版本不支持，原文件已保留') from None


def save(workspace,value):
    clean=validate(value)
    root=Path(workspace); root.mkdir(parents=True,exist_ok=True)
    # SQLite 的跨进程写锁覆盖读旧值、备份和替换全过程；进程退出自动释放。
    with closing(sqlite3.connect(root/'daily-report-save.lock',timeout=5)) as lock:
        lock.execute('BEGIN IMMEDIATE')
        path=root/'daily-report.json'
        if path.exists():
            old=load(root)  # 损坏原件不被普通保存静默覆盖。
            if old==clean:return clean
            backup=root/'daily-report-backups';backup.mkdir(exist_ok=True)
            (backup/(uuid.uuid4().hex+'.json')).write_bytes(path.read_bytes())
        pending=root/('daily-report-'+uuid.uuid4().hex+'.pending')
        with pending.open('x',encoding='utf-8') as stream:
            stream.write(json.dumps(clean,ensure_ascii=False,indent=2));stream.flush();os.fsync(stream.fileno())
        validate(json.loads(pending.read_text(encoding='utf-8')))
        pending.replace(path)
    return clean
