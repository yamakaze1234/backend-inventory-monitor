"""从持久回执继续投递已结束的订单任务；不保存临时会话 Webhook。"""
import json
import threading

from ops_command_api import CommandError
from ops_replies import render


def configured_target(root, robot, org):
    from order_bot_config import load, queue_state
    config = load(root)
    if not config.get('state_root'):
        return None
    target = queue_state(config['state_root'])['target']
    profile, group = target.get('profile'), target.get('groupId')
    if (not isinstance(profile,str) or profile.partition(':')[0]!=org
            or target.get('robotCode')!=robot or not isinstance(group,str) or not group.startswith('cid')):
        return None
    return target


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
            if self.halt.is_set() or not may_send():
                return
            actor = tuple(json.loads(raw))
            if actor[:2]!=(self.api.org_id,self.target['groupId']):
                continue
            try:
                # 对已删除的权限、跨组请求及仍在执行的任务一律不投递。
                # 若接收端在首条回复前中断，等任务至少存在一分钟再补最终结果。
                if initial not in ('sent','failed','unknown'):
                    from time import time
                    task = self.api.business.task(self.api.external_id(actor,ident))
                    if not task or time()-task.get('created_at',time())<60:
                        continue
                claim = self.api.delivery(actor,ident,dict(phase='final',state='claim'))
            except CommandError:
                continue
            if claim.get('claimed') is not True:
                continue
            state = 'unknown'
            try:
                target = dict(profile=self.target['profile'],group_id=actor[1],group_name=self.target['groupName'],
                              robot_code=self.target['robotCode'],robot_name=self.target['robotName'])
                value = self.sender(target,dict(msgtype='text',text=dict(content=render(claim['reply']))))
                if value.get('status') in ('sent','failed','unknown'):
                    state = value['status']
            except Exception:
                pass
            try:
                self.api.delivery(actor,ident,dict(phase='final',state=state))
            except CommandError:
                # 已领取的回执仍标为状态待核对，避免重复投递。
                continue
