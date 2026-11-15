"""只用临时工作区验证固定点、升级兼容和多群回执，不访问生产或发送消息。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
import json
import runpy
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from inventory_patrol_window import load_schedule, save_schedule, ready, save, allowed
from notification_robots import RobotStore

REPO=Path(__file__).resolve().parents[1]
MATCH=REPO/'integrations/order_backend/matching'


def at(clock):
    return datetime.fromisoformat('2026-11-15T'+clock+':00+08:00').timestamp()


class FixedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_defaults_preserve_interval_and_window(self):
        self.assertEqual(load_schedule(self.root,'inventory')['mode'],'interval')
        save(self.root,'inventory',False,'09:00','18:00')
        self.assertFalse(ready(self.root,'inventory',True,at('08:00'),claim=True))
        self.assertTrue(ready(self.root,'inventory',True,at('09:00'),claim=True))
        self.assertFalse(ready(self.root,'inventory',False,at('09:00'),claim=True))

    def test_fixed_ignores_old_interval_and_window_but_claims_once(self):
        save(self.root,'inventory',False,'18:30','23:00')
        save_schedule(self.root,'inventory','daily',['09:00','12:00','09:00'],at('08:00'))
        self.assertTrue(allowed(self.root,'inventory',at('09:00')))
        self.assertFalse(ready(self.root,'inventory',True,at('08:59')))
        self.assertTrue(ready(self.root,'inventory',False,at('09:00')))
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=list(pool.map(lambda _:ready(self.root,'inventory',False,at('09:01'),claim=True),range(2)))
        self.assertEqual(sorted(values),[False,True])
        self.assertFalse(ready(self.root,'inventory',True,at('09:02'),claim=True))
        self.assertTrue(ready(self.root,'inventory',False,at('12:00'),claim=True))

    def test_missed_and_saved_past_slots_are_not_replayed(self):
        save_schedule(self.root,'shipping','daily',['09:00','12:00'],at('09:01'))
        self.assertFalse(ready(self.root,'shipping',True,at('09:02'),claim=True))
        self.assertFalse(ready(self.root,'shipping',True,at('12:05'),claim=True))
        self.assertTrue(ready(self.root,'shipping',False,at('09:00')+86400,claim=True))

    def test_switching_preserves_claim_and_plan_independence(self):
        for key in ('orders','douyin:a','douyin:b'):
            save_schedule(self.root,key,'daily',['09:00'],at('08:00'))
        self.assertTrue(ready(self.root,'orders',False,at('09:00'),claim=True))
        save_schedule(self.root,'orders','interval',at=at('09:01'))
        save_schedule(self.root,'orders','daily',['09:00'],at('09:01'))
        self.assertFalse(ready(self.root,'orders',True,at('09:02'),claim=True))
        self.assertTrue(ready(self.root,'douyin:a',False,at('09:02'),claim=True))
        self.assertTrue(ready(self.root,'douyin:b',False,at('09:02'),claim=True))

    def test_invalid_save_and_corrupt_fail_closed(self):
        save_schedule(self.root,'inventory','daily',['09:00'],at('08:00'))
        for values in ([],['24:00'],['9:00'],['x'],['09:00']*25):
            with self.assertRaises(ValueError):save_schedule(self.root,'inventory','daily',values)
        self.assertEqual(load_schedule(self.root,'inventory')['times'],['09:00'])
        with closing(sqlite3.connect(self.root/'patrol-windows.sqlite3')) as db,db:
            db.execute("UPDATE schedules SET data='broken'")
        self.assertFalse(ready(self.root,'inventory',True,at('09:00'),claim=True))

    def test_sql_fixed_point_overrides_next_check_and_failure_does_not_repeat(self):
        from inventory_monitor import InventoryService
        from inventory_sql_service import SqlWorker
        service=InventoryService(self.root);worker=SqlWorker(service)
        source=Mock(scope='fixture',legacy_text=False);worker.source=source
        source.read_bundle.side_effect=ValueError('模拟查询失败')
        service.pending_warehouse_request=Mock(return_value=None)
        worker.refresh_requested=True
        with service.db() as db:service.put(db,'next_check',at('18:00'))
        save_schedule(self.root,'inventory','daily',['09:00'],at('08:00'))
        worker.step(at('08:59'));source.read_bundle.assert_not_called()
        worker.step(at('09:00'));self.assertEqual(source.read_bundle.call_count,1)
        worker.retry_at=0
        worker.step(at('09:02'));self.assertEqual(source.read_bundle.call_count,1)

    def test_ui_mode_and_multiple_times(self):
        from inventory_schedule_view import schedule_form
        form=schedule_form(self.root,'orders')
        self.assertEqual(form['values']['mode'],'interval')
        self.assertEqual(form['fields'][1]['kind'],'times')
        self.assertEqual(form['fields'][1]['limit'],24)

    def test_shop_interval_form_accepts_string_and_keeps_queue_serialization(self):
        from inventory_business_actions import patrol_schedule
        b=Mock();b.service.root=self.root
        patrol_schedule(b,dict(key='douyin:a',mode='interval',minutes='60'))
        operation,ident=b.douyin.submit.call_args.args
        self.assertEqual(ident,'a');self.assertEqual(operation['operation'],'interval:3600')
        b.douyin.store.update_interval.assert_not_called()
        for value in (True,'14','1.5','bad'):
            with self.assertRaises(ValueError):patrol_schedule(b,dict(key='douyin:a',mode='interval',minutes=value))


class FanoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=RobotStore(self.root)
        with patch.object(sys,'path',[str(MATCH),*sys.path]):
            self.mod=runpy.run_path(str(MATCH/'patrol_notifications.py'))
        self.deliver=self.mod['deliver']
        self.patch=patch.dict(self.deliver.__globals__,configured_store=lambda channel:self.store)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.db=sqlite3.connect(self.root/'queue.sqlite3',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,created REAL,delivery TEXT,evidence TEXT)')

    def recipient(self,name,channels=('shipping',)):
        target=dict(profile='fixture',group_id='cid-'+name,group_name=name,robot_code='ding-fixture',robot_name='fixture')
        return self.store.save(name,channels=list(channels),mode='enterprise',enterprise=target,enabled=True)

    def job(self,ident):
        self.db.execute('INSERT INTO jobs VALUES (?,?,?,NULL)',(ident,time.time()+1,'pending'))
        return self.db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()

    def state(self,ident):
        return self.db.execute('SELECT delivery FROM jobs WHERE id=?',(ident,)).fetchone()[0]

    def test_multigroup_scopes_partial_and_unknown_never_repeat(self):
        self.recipient('a');self.recipient('b');self.recipient('other',('order_patrol',))
        def send(args,**kwargs):
            group=args[args.index('--groups')+1]
            if group=='cid-b':raise TimeoutError('模拟回执未知')
            return Mock(returncode=0,stdout='{"succeededCount":1,"failedCount":0}')
        runner=Mock(side_effect=send)
        self.assertTrue(self.deliver(self.db,self.job('one'),'shipping','text','dws',runner,'stable'))
        self.assertEqual(runner.call_count,2);self.assertEqual(self.state('one'),'partial')
        self.deliver(self.db,self.job('two'),'shipping','text','dws',runner,'stable')
        self.assertEqual(runner.call_count,2);self.assertEqual(self.state('two'),'partial')
        self.assertEqual({r['status'] for r in self.store.recent_receipts()},{'sent','unknown'})

    def test_disable_all_does_not_fall_back(self):
        ident=self.recipient('a');self.store.set_enabled(ident,False)
        runner=Mock()
        self.deliver(self.db,self.job('one'),'shipping','text','dws',runner)
        runner.assert_not_called();self.assertEqual(self.state('one'),'suppressed')
        with self.store.db() as db:
            self.assertTrue(db.execute("SELECT 1 FROM migrations WHERE id='managed:shipping'").fetchone())

    def test_audience_snapshot_does_not_add_new_group_mid_message(self):
        self.recipient('a')
        def send(*args,**kwargs):
            if runner.call_count==1:self.recipient('b')
            return Mock(returncode=0,stdout='{"succeededCount":1,"failedCount":0}')
        runner=Mock(side_effect=send)
        self.deliver(self.db,self.job('one'),'shipping','x'*1600,'dws',runner)
        self.assertEqual(runner.call_count,2)
        self.assertEqual(self.state('one'),'sent')
        self.assertTrue(all(c.args[0][c.args[0].index('--groups')+1]=='cid-a' for c in runner.call_args_list))

    def test_legacy_workspace_has_no_forced_migration(self):
        fn=self.mod['configured_store']
        # 此函数和 deliver 共用 globals；用独立载入检查真实配置发现。
        fn=runpy.run_path(str(MATCH/'patrol_notifications.py'))['configured_store']
        with patch.dict(fn.__globals__,department_workspace=lambda:self.root):
            self.assertIsNone(fn('shipping'))
            self.recipient('a')
            self.assertIsNotNone(fn('shipping'))
            self.assertIsNone(fn('order_patrol'))

    def test_bundled_notification_sources_remain_identical(self):
        for name in ('notification_robots.py','notification_enterprise.py','monitor_secret_store.py'):
            self.assertEqual((REPO/'app/src'/name).read_bytes(),(MATCH/name).read_bytes(),name)


class QueueFixedTests(unittest.TestCase):
    def test_daily_order_is_not_cancelled_by_legacy_evening_window(self):
        # 载入真实领取代码，外部匹配依赖全部替换为无业务能力的对象。
        import types
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            modules=dict(patrol_service=types.SimpleNamespace(single_instance=Mock()),
                patrol_worker=types.SimpleNamespace(BatchMatchAdapter=Mock(),PatrolWorker=Mock()),
                task_store=types.SimpleNamespace(TaskStore=Mock()),robot_config=types.SimpleNamespace(get_config=Mock()),
                order_department_runtime=types.SimpleNamespace(settings=lambda:dict(state_root=tmp,dws_path='fixture',shops=[])))
            with patch.object(sys,'path',[str(MATCH),*sys.path]),patch.dict(sys.modules,modules):
                module=runpy.run_path(str(MATCH/'group_order_bot.py'))
            claim=module['claim_next_job']
            db=sqlite3.connect(root/'jobs.sqlite3',isolation_level=None);db.row_factory=sqlite3.Row
            try:
                db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE jobs(id TEXT,state TEXT,delivery TEXT,updated REAL,evidence TEXT,kind TEXT,origin TEXT,created REAL);')
                db.execute("INSERT INTO settings VALUES('matchPatrolWindow',?)",(json.dumps(dict(start='18:30',end='00:00',intervalMinutes=120)),))
                save_schedule(root,'orders','daily',['09:00'],at('08:00'))
                db.execute("INSERT INTO jobs VALUES('fixed','queued','pending',0,'','today','schedule',?)",(at('09:00'),))
                with patch.dict(claim.__globals__,department_workspace=lambda:root):
                    self.assertEqual(claim(db,at('09:01'))['id'],'fixed')
                    db.execute("INSERT INTO jobs VALUES('yesterday','queued','pending',0,'','today','schedule',?)",(at('09:00')-86400,))
                    self.assertIsNone(claim(db,at('09:02')))
                    self.assertEqual(db.execute("SELECT state FROM jobs WHERE id='yesterday'").fetchone()[0],'cancelled')
                    save_schedule(root,'orders','interval',at=at('09:02'))
                    save_schedule(root,'shipping','daily',['09:00'],at('08:00'))
                    db.execute("INSERT INTO jobs VALUES('shipping','queued','pending',0,'','shipping','schedule',?)",(at('09:00'),))
                    self.assertEqual(claim(db,at('09:03'))['id'],'shipping')
            finally:db.close()
