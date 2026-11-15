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
    value = load(root, key)
    if not value['valid']:
        return False
    if value['allDay']:
        return True
    clock = datetime.fromtimestamp(time.time() if at is None else at, CN).strftime('%H:%M')
    start, end = value['start'], value['end']
    return start <= clock < end if start < end else clock >= start or clock < end


def department_workspace():
    path = os.environ.get('ORDER_BOT_CONFIG')
    if not path:
        pointer = Path(__file__).with_name('order-department.json')
        if pointer.is_file():
            path = json.loads(pointer.read_text(encoding='utf-8'))['configPath']
    return Path(path).resolve().parent if path else None
