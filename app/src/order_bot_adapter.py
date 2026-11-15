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

class OrderBotAdapter:
    def __init__(self, match_root=None, control_root=None, state_root=None, runner=None, workspace_root=None):
        # 未配置的新电脑不扫描个人目录，也不自动启动任何机器人。
        self.workspace_root=Path(workspace_root).resolve() if workspace_root is not None else None
        self.match_root=Path(match_root).resolve() if match_root is not None else None
        self.control_root=Path(control_root).resolve() if control_root is not None else None
        self.state_root=Path(state_root).resolve() if state_root is not None else (self.workspace_root/'order_matching' if self.workspace_root else Path.home()/'.inventory-unconfigured-orders')
        self.runner = runner or subprocess.run
        self.lock = threading.Lock()
        self._refresh_configuration()

    def _refresh_configuration(self):
        if self.workspace_root is None:return {}
        from order_bot_config import load
        value=load(self.workspace_root)
        if value:
            self.match_root=Path(value['match_root']);self.control_root=Path(value['control_root']);self.state_root=Path(value['state_root'])
        return value

    def configuration(self):
        if self.workspace_root is None:return dict(configured=False,deployed=False,shops=[],target={},message='当前预览未启用订单配置')
        from order_bot_config import describe
        self._refresh_configuration()
        return describe(self.workspace_root)

    def save_configuration(self, values):
        if self.workspace_root is None:raise ValueError('当前服务未启用订单配置')
        from order_bot_config import save
        if not self.lock.acquire(blocking=False):raise ValueError('订单操作正在执行，请稍后保存')
        try:save(self.workspace_root,values);self._refresh_configuration();return self.configuration()
        finally:self.lock.release()

    def deploy_backend(self):
        if self.workspace_root is None:raise ValueError('当前服务未启用订单配置')
        from order_bot_config import deploy
        if not self.lock.acquire(blocking=False):raise ValueError('订单操作正在执行，请稍后部署')
        try:return deploy(self.workspace_root)
        finally:self.lock.release()

    def snapshot(self, page=0, size=25):
        """只读连接不建表、不启动工作进程、不触发旧版记录清理。"""
        config=self._refresh_configuration()
        shops=config.get('shops',[])
        page, size = max(0, int(page)), min(100, max(1, int(size)))
        database = self.state_root / 'jobs.sqlite3'
        if not database.is_file():
            return dict(available=False, workerOnline=False, jobs=[], total=0, configured=bool(config),shops=shops,message='请先在订单匹配设置中配置独立后端与 Stream 机器人' if not config else '尚未启动本部门订单机器人')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('heartbeat','schedulePaused','serviceEnabled','stopRequested','matchPatrolWindow','robotConfig')").fetchall())
            total = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            rows = db.execute('SELECT id,kind,number,shop,state,created,delivery FROM jobs ORDER BY created DESC LIMIT ? OFFSET ?', (size, page * size)).fetchall()
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','unknown')").fetchone()[0]
        from order_bot_config import patrol_window_ready, patrol_window_view
        window = None
        if patrol_window_ready(config):
            # 只读取旧路由中的间隔作为默认值，不返回机器人配置。
            legacy = json.loads(settings.get('robotConfig') or '{}').get('intervalMinutes')
            window = patrol_window_view(settings.get('matchPatrolWindow'),legacy)
        return dict(available=True,configured=bool(config),shops=shops,patrolWindow=window, workerOnline=time.time()-float(settings.get('heartbeat','0')) < 90,
                    schedulePaused=settings.get('schedulePaused') == '1', serviceEnabled=settings.get('serviceEnabled','1') != '0',
                    stopRequested=settings.get('stopRequested') == '1', jobs=[dict(r) for r in rows], total=total, active=active)

    def ops_supported(self):
        # 新版必须同时具备队列路由检查及切换和投递共用的跨进程锁。
        from order_bot_config import deployed, bundle_root
        config = self._refresh_configuration()
        if not config or not deployed(self.workspace_root, config):
            return False
        source=bundle_root()
        required=((self.match_root/'app/src','matching','group_order_bot.py'),
                  (self.control_root,'control','robot_desktop_control.py'),
                  (self.control_root,'control','ops_order_route_lock.py'))
        return all((parent/name).is_file() and (parent/name).read_bytes()==(source/kind/name).read_bytes()
                   for parent,kind,name in required)

    def ops_order_status(self, number_type, number, shops):
        return self.action('ops-order-status', dict(number_type=number_type, number=number, shops=shops))

    def ops_task(self, external_id):
        # 回读仅按外部请求键；只投影状态与数量，订单明细和内部错误不进群。
        self._refresh_configuration()
        path = self.state_root / 'jobs.sqlite3'
        if not path.is_file():
            return None
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            row = db.execute("""SELECT j.id,j.state,j.result,e.shop,e.number,j.origin,j.created,
                                e.rowid=(SELECT MIN(o.rowid) FROM ops_external_jobs o WHERE o.job_id=j.id)
                                FROM ops_external_jobs e
                                JOIN jobs j ON j.id=e.job_id WHERE e.request_id=?""", (external_id,)).fetchone()
        if not row:
            return None
        task_id, state, raw, shop, number, origin, created, first = row
        report = json.loads(raw) if raw else {}
        shops = report.get('shops', [])
        manual = report.get('manualItems', [])
        # 手动任务记录逐项结果；巡检现有后端只提供可确认的商品明细行数。
        stats = {}
        if shops and all(isinstance(s.get('opsSummary'), dict) for s in shops):
            stats = {k: sum(s['opsSummary'][k] for s in shops) for k in ('success', 'skipped', 'review')}
            stats['unit'] = '单'
        elif shops:
            stats = dict(success=sum(s['verified'] for s in shops), skipped=sum(s['alreadyLinked'] for s in shops),
                         review=sum(len(s['exceptions']) for s in shops), unit='明细行')
            if any(not s.get('scan', {}).get('complete') for s in shops):
                stats['reason'] = '部分订单扫描未完成'
        elif manual:
            stats = dict(success=sum(r.get('status') == 'verified' for r in manual),
                         skipped=sum(r.get('status') in ('skipped', 'already_linked') for r in manual),
                         review=sum(r.get('status') in ('unknown', 'needs_review') for r in manual), unit='明细行')
        return dict(task_id=task_id, status=state if state in ('queued','running','completed','needs_review','failed','unknown') else 'unknown',
                    shop_id=shop, order_number=number, created_at=created, final_reply_owned=origin == 'ops-command' and bool(first), **stats)

    def action(self, action, payload=None):
        """参数不进入命令字符串；超时不自动重试，避免未知写入重复入队。"""
        if action not in {'engine-check','start','pause','resume','stop','finish-stop','patrol','order','spu','number','detail','config','discover-robots','save-robot','save-patrol-window','ops-enqueue','ops-order-status','shipping','shipping-on','shipping-off','shipping-interval'}:
            raise ValueError('不支持的机器人操作')
        config=self._refresh_configuration()
        from order_bot_config import deployed,config_path
        if not config or self.workspace_root is None:raise ValueError('请先单独配置订单匹配运行环境')
        if not deployed(self.workspace_root,config):raise ValueError('请先停止旧订单服务并部署本部门兼容后端')
        if payload and payload.get('shop') and payload['shop'] not in {s['id'] for s in config['shops']}:raise ValueError('店铺不在本部门匹配范围')
        python = Path(config['python_path'])
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
elif action=='shipping-interval':
    # 发货周期独立保存，不改订单匹配周期或提醒路由。
    minutes=int(request['payload']['minutes'])
    if not 15<=minutes<=1440: raise ValueError('发货周期须为 15 至 1440 分钟')
    control.settings({'shippingIntervalMinutes':str(minutes)},root)
    result={'message':'发货监控周期已保存'}
elif action=='save-patrol-window':
    payload=request['payload']
    result=control.bot.save_patrol_window(root,payload['start'],payload['end'],payload['intervalMinutes'])
elif action=='ops-order-status':
    # 固定模块由该部门订单后端提供；接口升级前不回退为猜测或模糊匹配。
    if request['status_sdk']:
        sys.path.insert(0,request['status_sdk'])
    import order_status_bridge
    payload=request['payload']
    result=order_status_bridge.query_verified_orders(payload['number_type'],payload['number'],payload['shops'])
elif action=='ops-enqueue':
    payload=request['payload']
    # 即使工作区在核验后切换，旧请求也不能进入新队列。
    if root.resolve()!=Path(payload['expected_root']).resolve():
        result={'status':'unavailable'}
    else:
        result=control.bot.enqueue(payload['kind'],number=payload.get('number',''),shop=payload['shop'],
                                   origin='ops-command',root=root,external_id=payload['external_id'],
                                   expected_route=payload['expected_route'])
elif action=='start':
    if not control.finish_switch(root):
        control.start_services(root)
    result={'message':'已请求启动，请以工作进程心跳为准'}
elif action=='detail':
    import sqlite3,contextlib
    with contextlib.closing(sqlite3.connect((root/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT result,state,evidence FROM jobs WHERE id=?',(request['payload'].get('id',''),)).fetchone()
    if not row:
        raise ValueError('任务记录不存在')
    result={'replyText':control.bot.message_for(json.loads(row[0])) if row[0] else
            row[2] if row[1]=='cancelled' and row[2] else '任务尚未生成结果，请稍后刷新'}
else:
    result=control.handle(action,root=root,payload=request['payload'])
print(json.dumps(result,ensure_ascii=False))
'''
            # 履约查询使用独立固定版本 SDK，不替换订单写入进程的依赖。
            status_sdk = self.workspace_root/'order-status-sdk'
            result = self.runner([str(python), '-B', '-X', 'utf8', '-c', script],
                env=dict(os.environ,ORDER_BOT_CONFIG=str(config_path(self.workspace_root).resolve())),
                input=json.dumps(dict(control=str(self.control_root),state=str(self.state_root),action=action,
                                      status_sdk=str(status_sdk) if action=='ops-order-status' and status_sdk.is_dir() else '',
                                      payload=payload or {})),
                capture_output=True, text=True, encoding='utf-8', timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError('机器人操作未完成，请查看原机器人运行记录；未自动重试')
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise ValueError('操作超时，结果尚未确认。请刷新任务记录，勿重复提交') from None
        finally:
            self.lock.release()
