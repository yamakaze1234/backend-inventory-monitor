"""从持久回执继续投递已结束的订单任务；不保存临时会话 Webhook。"""
from contextlib import closing, nullcontext
from functools import lru_cache
import json
from pathlib import Path
import runpy
import sqlite3
import threading

from ops_command_api import CommandError
from ops_replies import render


ROUTE_KEYS = ('profile','groupId','robotCode','appId')
def route_identity(target):
    return tuple(target.get(key) for key in (*ROUTE_KEYS,'_stateRoot')) if target else ()


def configured_target(root, robot, org):
    from order_bot_config import PUBLIC_TARGET, load
    config = load(root)
    if not config.get('state_root'):
        return None
    state_root = Path(config['state_root']).resolve()
    queue = state_root/'jobs.sqlite3'
    if not queue.is_file():
        return None
    # 只读路由；切换待完成或队列暂时不可读时不使用旧群。
    with closing(sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        values = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('robotConfig','pendingRobotConfig')"))
    if values.get('pendingRobotConfig'):
        return None
    target = json.loads(values.get('robotConfig') or '{}')
    profile, group = target.get('profile'), target.get('groupId')
    if (not isinstance(profile,str) or profile.partition(':')[0]!=org
            or target.get('robotCode')!=robot or not isinstance(group,str) or not group.startswith('cid')):
        return None
    return {**{key:target[key] for key in PUBLIC_TARGET if key in target},'_stateRoot':str(state_root)}


@lru_cache(maxsize=1)
def _route_guard():
    # 桌面程序和兼容后端使用同一份跨进程锁实现。
    from order_bot_config import bundle_root
    return runpy.run_path(str(bundle_root()/'control/ops_order_route_lock.py'))['route_guard']


class OrderDelivery:
    def __init__(self, api, target, *, sender=None):
        from notification_enterprise import send_enterprise
        self.api, self.target, self.sender = api, target, sender or send_enterprise
        self.halt = threading.Event()
        self.cursor = 0

    def start(self, may_send):
        # 桌面重启后从 SQLite 找回任务；另一个进程即使误启动也抢不到同一回执。
        def loop():
            while not self.halt.wait(4):
                if may_send():
                    try:
                        self.tick(may_send=may_send)
                    except Exception:
                        # 数据库可能在更新；下次重读，已经领取的投递不自动重试。
                        continue
        threading.Thread(target=loop,name='ops-order-delivery',daemon=True).start()
        return self

    def close(self):
        self.halt.set()

    def tick(self, limit=32, *, may_send=lambda:True):
        with self.api.service.db() as db:
            rows = db.execute("""SELECT q.rowid,q.id,q.actor,i.state FROM ops_requests q
              LEFT JOIN ops_replies f ON f.robot=q.robot AND f.org=q.org AND f.id=q.id AND f.phase='final'
              LEFT JOIN ops_replies i ON i.robot=q.robot AND i.org=q.org AND i.id=q.id AND i.phase='initial'
              WHERE q.robot=? AND q.org=? AND q.rowid>? AND q.http IN (200,202) AND f.id IS NULL
                AND q.command IN ('order.match','order.patrol') ORDER BY q.rowid LIMIT ?""",
                (self.api.robot_id,self.api.org_id,self.cursor,limit)).fetchall()
        self.cursor = rows[-1][0] if len(rows)==limit else 0
        for _,ident,raw,initial in rows:
            if self.halt.is_set() or not may_send() or not self.api.business.order_route_current():
                return
            actor = tuple(json.loads(raw))
            if actor[:2]!=(self.api.org_id,self.target['groupId']):
                continue
            root=self.target.get('_stateRoot')
            try:
                with (_route_guard()(root) if root else nullcontext()):
                    self._send_final(actor,ident,initial,may_send)
            except sqlite3.OperationalError:
                # 切换或另一个发送器持锁时，下轮重读；本轮没有领取回执。
                return

    def _send_final(self,actor,ident,initial,may_send):
        if not may_send() or not self.api.business.order_route_current():
            return
        try:
            # 对已删除的权限、跨组请求及仍在执行的任务一律不投递。
            # 若接收端在首条回复前中断，等任务至少存在一分钟再补最终结果。
            if initial not in ('sent','failed','unknown'):
                from time import time
                task=self.api.business.task(self.api.external_id(actor,ident))
                if not task or time()-task.get('created_at',time())<60:
                    return
            claim=self.api.delivery(actor,ident,dict(phase='final',state='claim'),internal=True)
        except CommandError:
            return
        if claim.get('claimed') is not True or not may_send() or not self.api.business.order_route_current():
            return
        state='unknown'
        try:
            target=dict(profile=self.target['profile'],group_id=actor[1],group_name=self.target['groupName'],
                        robot_code=self.target['robotCode'],robot_name=self.target['robotName'])
            value=self.sender(target,dict(msgtype='text',text=dict(content=render(claim['reply']))))
            if value.get('status') in ('sent','failed','unknown'):
                state=value['status']
        except Exception:
            pass
        try:
            self.api.delivery(actor,ident,dict(phase='final',state=state),internal=True)
        except CommandError:
            # 已领取的回执仍标为状态待核对，避免重复投递。
            pass
