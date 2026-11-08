"""Non-secret bot routing settings, shared by worker and desktop host."""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from order_department_runtime import settings as department_settings
DEPARTMENT = department_settings()
STATE = Path(DEPARTMENT['state_root'])
# 新部门没有预选机器人，必须先通过 Stream 应用校验保存独立路由。
DEFAULT = {'robotName':'','robotCode':'','appId':'','groupName':'','groupId':'','profile':'','intervalMinutes':120}


def validate(config):
    if set(config) != set(DEFAULT): raise ValueError('机器人配置字段不完整')
    if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',config['appId']): raise ValueError('应用 ID 格式错误')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{5,100}',config['robotCode']): raise ValueError('机器人标识格式错误')
    if not re.fullmatch(r'cid[A-Za-z0-9+/=_-]{5,150}',config['groupId']): raise ValueError('群标识格式错误')
    if not isinstance(config['profile'],str) or not config['profile'].strip(): raise ValueError('请先选择当前组织账号')
    if type(config['intervalMinutes']) is not int or not 15 <= config['intervalMinutes'] <= 1440: raise ValueError('巡检间隔须为 15–1440 分钟')
    for key in ('robotName','groupName'):
        if not isinstance(config[key],str) or not 1 <= len(config[key]) <= 100: raise ValueError('名称无效')
    return config


def get_config(root=STATE):
    path=Path(root)/'jobs.sqlite3'
    if not path.exists(): raise ValueError('请先配置订单匹配的 Stream 机器人')
    with closing(sqlite3.connect(path,timeout=5)) as db:
        row=db.execute("SELECT value FROM settings WHERE key='robotConfig'").fetchone()
    if not row:raise ValueError('请先配置订单匹配的 Stream 机器人')
    return validate(json.loads(row[0]))
