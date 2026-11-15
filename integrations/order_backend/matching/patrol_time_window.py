"""各类自动巡检的独立北京时间段；未配置全天，配置损坏时停止自动启动。"""
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
import json
import os
import re
import sqlite3
import time

CN = timezone(timedelta(hours=8))
DEFAULT = dict(allDay=True, start='09:00', end='18:00', valid=True)


def validate(all_day, start, end):
    if type(all_day) is not bool:
        raise ValueError('请选择全天或指定时段')
    if all_day:
        return dict(DEFAULT)
    if any(not isinstance(t, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in (start, end)):
        raise ValueError('请输入有效的开始和结束时间（北京时间）')
    if start == end:
        raise ValueError('指定时段的开始和结束不能相同；全天请选择全天运行')
    return dict(allDay=False, start=start, end=end, valid=True)


def load(root, key):
    if root is None or not (Path(root) / 'patrol-windows.sqlite3').exists():
        return dict(DEFAULT)
    try:
        with closing(sqlite3.connect(Path(root) / 'patrol-windows.sqlite3', timeout=5)) as db:
            row = db.execute('SELECT data FROM windows WHERE key=?', (key,)).fetchone()
        if row is None:
            return dict(DEFAULT)
        value = json.loads(row[0])
        return validate(value['allDay'], value['start'], value['end'])
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return dict(DEFAULT, valid=False)


def save(root, key, all_day, start=None, end=None):
    value = validate(all_day, start, end)
    with closing(sqlite3.connect(Path(root) / 'patrol-windows.sqlite3', timeout=10)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS windows (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO windows VALUES (?,?)', (key, json.dumps(value)))
    return value


def allowed(root, key, at=None):
    schedule = load_schedule(root, key)
    if not schedule['valid']:
        return False
    if schedule['mode'] == 'daily':
        return True  # 固定时间点独立于旧间隔时段；已领取任务允许正常完成。
    value = load(root, key)
    if not value['valid']:
        return False
    if value['allDay']:
        return True
    clock = datetime.fromtimestamp(time.time() if at is None else at, CN).strftime('%H:%M')
    start, end = value['start'], value['end']
    return start <= clock < end if start < end else clock >= start or clock < end


def normalize_times(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 24:
        raise ValueError('请添加 1 至 24 个固定时间点')
    if any(not isinstance(t, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in values):
        raise ValueError('固定时间点须为 HH:MM（北京时间）')
    return sorted(set(values))


def load_schedule(root, key):
    default = dict(mode='interval', times=['09:00'], valid=True, since=0)
    path = Path(root) / 'patrol-windows.sqlite3' if root else None
    if not path or not path.exists():
        return default
    try:
        with closing(sqlite3.connect(path, timeout=5)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='schedules'").fetchone():
                return default
            row = db.execute('SELECT data FROM schedules WHERE key=?', (key,)).fetchone()
        if row is None:
            return default
        value = json.loads(row[0])
        if value['mode'] not in ('interval', 'daily'):
            raise ValueError('未知运行方式')
        return dict(mode=value['mode'], times=normalize_times(value['times']), since=float(value['since']), valid=True)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return dict(default, valid=False)


def save_schedule(root, key, mode, times=None, at=None):
    if mode not in ('interval', 'daily'):
        raise ValueError('请选择按间隔或固定时间点运行')
    previous = load_schedule(root, key)
    values = normalize_times(times) if mode == 'daily' else previous['times']
    value = dict(mode=mode, times=values, since=time.time() if at is None else at)
    # 重复保存相同计划不重新开放本日已领取时间点。
    if previous['valid'] and previous['mode'] == mode and previous['times'] == values:
        value['since'] = previous['since']
    with closing(sqlite3.connect(Path(root) / 'patrol-windows.sqlite3', timeout=10)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS windows (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS schedules (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO schedules VALUES (?,?)', (key, json.dumps(value)))
    return value


def ready(root, key, interval_due, at=None, *, claim=False):
    """固定点允许五分钟启动延迟；错过不补跑，领取落盘避免重启重复执行。"""
    at = time.time() if at is None else at
    value = load_schedule(root, key)
    if not value['valid']:
        return False
    if value['mode'] == 'interval':
        return interval_due and allowed(root, key, at)
    current = datetime.fromtimestamp(at, CN)
    slots = [current.replace(hour=int(t[:2]), minute=int(t[3:]), second=0, microsecond=0).timestamp()
             for t in value['times']]
    slots = [slot for slot in slots if value['since'] <= slot <= at < slot + 300]
    if not slots:
        return False
    slot = max(slots)
    with closing(sqlite3.connect(Path(root) / 'patrol-windows.sqlite3', timeout=10)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS schedule_claims (key TEXT, slot REAL, PRIMARY KEY(key,slot))')
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM schedule_claims WHERE key=? AND slot=?', (key, slot)).fetchone():
            return False
        if claim:
            db.execute('INSERT INTO schedule_claims VALUES (?,?)', (key, slot))
        return True


def department_workspace():
    path = os.environ.get('ORDER_BOT_CONFIG')
    if not path:
        pointer = Path(__file__).with_name('order-department.json')
        if pointer.is_file():
            path = json.loads(pointer.read_text(encoding='utf-8'))['configPath']
    return Path(path).resolve().parent if path else None
