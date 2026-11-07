"""订单机器人适配：读取原队列，写操作只交给原项目固定入口。"""
from __future__ import annotations

import json
from contextlib import closing
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

MATCH_ROOT = Path(r'D:\工作\匹配\抖店SKU查询\抖店SKU查询')
CONTROL_ROOT = Path(r'D:\工作\PythonProject\数据分析\机器人日报')
STATE_ROOT = Path(r'D:\工作\匹配\巡检运行数据\群机器人')


class OrderBotAdapter:
    def __init__(self, match_root=MATCH_ROOT, control_root=CONTROL_ROOT, state_root=STATE_ROOT, runner=None):
        self.match_root, self.control_root, self.state_root = map(Path, (match_root, control_root, state_root))
        self.runner = runner or subprocess.run
        self.lock = threading.Lock()

    def snapshot(self, page=0, size=25):
        """只读连接不建表、不启动工作进程、不触发旧版记录清理。"""
        page, size = max(0, int(page)), min(100, max(1, int(size)))
        database = self.state_root / 'jobs.sqlite3'
        if not database.is_file():
            return dict(available=False, workerOnline=False, jobs=[], total=0, message='尚未找到订单机器人运行记录')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('heartbeat','schedulePaused','serviceEnabled','stopRequested')").fetchall())
            total = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            rows = db.execute('SELECT id,kind,number,shop,state,created,delivery FROM jobs ORDER BY created DESC LIMIT ? OFFSET ?', (size, page * size)).fetchall()
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','unknown')").fetchone()[0]
        return dict(available=True, workerOnline=time.time()-float(settings.get('heartbeat','0')) < 90,
                    schedulePaused=settings.get('schedulePaused') == '1', serviceEnabled=settings.get('serviceEnabled','1') != '0',
                    stopRequested=settings.get('stopRequested') == '1', jobs=[dict(r) for r in rows], total=total, active=active)

    def action(self, action, payload=None):
        """参数不进入命令字符串；超时不自动重试，避免未知写入重复入队。"""
        if action not in {'start','pause','resume','stop','finish-stop','patrol','order','spu','number','detail'}:
            raise ValueError('不支持的机器人操作')
        python = self.match_root / '.runtime-tiny-tools-016/Scripts/python.exe'
        if not python.is_file() or not (self.control_root / 'robot_desktop_control.py').is_file():
            raise ValueError('订单机器人依赖未安装，请检查匹配项目与机器人日报源码位置')
        if not self.lock.acquire(blocking=False):
            raise ValueError('机器人操作正在执行，请稍后查看结果')
        try:
            # 原适配器固定业务路径，独立子进程避免与库存模块同名依赖相互污染。
            script = '''import json,sys
from pathlib import Path
request=json.load(sys.stdin)
sys.path.insert(0,request['control'])
import robot_desktop_control as control
action=request['action']
root=Path(request['state'])
if action in ('order','spu','number'):
    payload=request['payload']
    result=control.bot.enqueue(action,number=payload.get('number',''),shop=payload.get('shop',''),root=root)
elif action=='start':
    if not control.finish_switch(root):
        control.settings({'serviceEnabled':'1','stopRequested':'0'},root)
        control.ensure()
    result={'message':'已请求启动，请以工作进程心跳为准'}
elif action=='detail':
    import sqlite3,contextlib
    with contextlib.closing(sqlite3.connect((root/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT result FROM jobs WHERE id=?',(request['payload'].get('id',''),)).fetchone()
    if not row:
        raise ValueError('任务记录不存在')
    result={'replyText':control.bot.message_for(json.loads(row[0])) if row[0] else '任务尚未生成结果，请稍后刷新'}
else:
    result=control.handle(action,root=root)
print(json.dumps(result,ensure_ascii=False))
'''
            result = self.runner([str(python), '-B', '-X', 'utf8', '-c', script],
                input=json.dumps(dict(control=str(self.control_root), state=str(self.state_root), action=action, payload=payload or {})),
                capture_output=True, text=True, encoding='utf-8', timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError('机器人操作未完成，请查看原机器人运行记录；未自动重试')
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise ValueError('操作超时，结果尚未确认。请刷新任务记录，勿重复提交') from None
        finally:
            self.lock.release()
