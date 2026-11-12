"""跨进程保护群切换和最终投递，不占用订单任务数据库。"""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def route_guard(root):
    # 单独锁只协调群路由；网络投递期间，订单队列仍可以运行。
    with closing(sqlite3.connect(Path(root)/'ops-route-lock.sqlite3',timeout=2,isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        yield
