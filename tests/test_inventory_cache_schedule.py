"""隔离验证自定义缓存范围、定期时间及任务保护，不清理真实业务数据。"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from inventory_monitor import InventoryService
from inventory_cache import preview,execute
from inventory_cache_schedule import TZ,save_settings,settings,run_due,next_time,legacy_managed


class CacheScheduleTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'outputs/cache-schedule-tests'
        base.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=base))
        self.service=InventoryService(self.root)
        self.now=datetime(2026,11,8,8,tzinfo=TZ).timestamp()
        self.trash=self.root/'fake-trash';self.trash.mkdir()
        with self.service.db() as db:
            self.service.put(db,'daily_source_root',str(self.root))
            db.execute('INSERT INTO messages VALUES (?,?,?,?)',('old',None,'{}',self.now-20*86400))
            for status in ('sent','pending','unknown'):
                db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',(status,'{}',self.now-20*86400,status,0,0))

    def recycle(self,path):path.replace(self.trash/path.name)

    def config(self,**kw):
        return dict(dict(enabled=True,time='09:00',period_days=3,retention_days=12,scopes=['messages']),**kw)

    def test_custom_scope_and_days_do_not_delete_unselected_or_unfinished(self):
        plan=preview(self.service,12,now=self.now,scopes=['events'])
        result=execute(self.service,plan,recycler=self.recycle,now=self.now)
        self.assertEqual(result['deleted_rows'],1)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            self.assertEqual(set(r[0] for r in db.execute('SELECT status FROM events')),{'pending','unknown'})

    def test_preview_expiry_and_invalid_ranges(self):
        plan=preview(self.service,12,now=self.now,scopes=['messages'])
        with self.assertRaisesRegex(ValueError,'30分钟'):
            execute(self.service,plan,recycler=self.recycle,now=self.now+1801)
        for scopes in ([],['credentials'],['messages','anything']):
            with self.assertRaises(ValueError):preview(self.service,12,now=self.now,scopes=scopes)
        for values in (self.config(time='24:00'),self.config(period_days=0),self.config(retention_days=-1)):
            with self.assertRaises(ValueError):save_settings(self.service,values,now=self.now)
        self.assertFalse(settings(self.service)['configured'])

    def test_scheduled_cleanup_claims_once_and_keeps_interval(self):
        save_settings(self.service,self.config(),now=self.now)
        due=datetime(2026,11,8,9,tzinfo=TZ).timestamp()
        self.assertEqual(settings(self.service)['state']['next_run'],due)
        self.assertIsNone(run_due(self.service,now=due-1,recycler=self.recycle))
        result=run_due(self.service,now=due,recycler=self.recycle)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['deleted_rows'],1)
        self.assertEqual(settings(self.service)['state']['next_run'],datetime(2026,11,11,9,tzinfo=TZ).timestamp())
        self.assertIsNone(run_due(self.service,now=due,recycler=self.recycle))
        # 新对象也读取同一计划，重启不立即再执行。
        restarted=InventoryService(self.root)
        self.assertIsNone(run_due(restarted,now=due+60,recycler=self.recycle))

    def test_busy_collection_defers_without_consuming_schedule(self):
        save_settings(self.service,self.config(),now=self.now)
        due=settings(self.service)['state']['next_run']
        runtime=SimpleNamespace(collection_lock=threading.Lock(),job_lock=threading.Lock(),
                                pending_collections={'shop'},running_jobs={},commands=queue.Queue())
        result=run_due(self.service,runtime=runtime,now=due,recycler=self.recycle)
        self.assertEqual(result['status'],'deferred')
        self.assertEqual(settings(self.service)['state']['next_run'],due)
        runtime.pending_collections.clear()
        self.assertEqual(run_due(self.service,runtime=runtime,now=due+1,recycler=self.recycle)['status'],'completed')

    def test_recycle_failure_retains_database_and_records_error(self):
        save_settings(self.service,self.config(),now=self.now)
        result=run_due(self.service,now=self.now+3600,recycler=Mock(side_effect=OSError('denied')))
        self.assertEqual(result['status'],'partial')
        with self.service.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],1)

    def test_failure_retries_three_times_and_preserves_normal_schedule(self):
        save_settings(self.service,self.config(period_days=365),now=self.now)
        due=settings(self.service)['state']['next_run']
        failed=Mock(side_effect=OSError('recycle unavailable'))
        normal=None
        for attempt in range(1,5):
            at=due+(attempt-1)*300
            state=run_due(self.service,now=at,recycler=failed)
            normal=normal or state['scheduled_next']
            self.assertEqual(state['scheduled_next'],normal)
            self.assertEqual(state['attempts'],attempt)
            self.assertEqual(state['next_run'],at+300 if attempt<4 else normal)
        self.assertTrue(state['retry_exhausted']);self.assertFalse(state['retryable'])

    def test_successful_retry_does_not_shift_normal_schedule(self):
        save_settings(self.service,self.config(),now=self.now)
        due=settings(self.service)['state']['next_run']
        failed=run_due(self.service,now=due,recycler=Mock(side_effect=OSError('denied')))
        success=run_due(self.service,now=due+300,recycler=self.recycle)
        self.assertEqual(success['status'],'completed')
        self.assertEqual(success['next_run'],failed['scheduled_next'])

    def test_stale_running_claim_recovers_after_process_interruption(self):
        save_settings(self.service,self.config(),now=self.now)
        due=settings(self.service)['state']['next_run']
        with patch('inventory_cache_schedule.execute',side_effect=SystemExit('simulated interruption')):
            with self.assertRaises(SystemExit):run_due(self.service,now=due,recycler=self.recycle)
        self.assertEqual(settings(self.service)['state']['status'],'running')
        self.assertIsNone(run_due(self.service,now=due+299,recycler=self.recycle))
        recovered=run_due(self.service,now=due+301,recycler=self.recycle)
        self.assertEqual(recovered['status'],'completed');self.assertEqual(recovered['attempts'],2)
        self.assertEqual(recovered['next_run'],datetime(2026,11,11,9,tzinfo=TZ).timestamp())

    def test_disabled_new_policy_replaces_hidden_legacy_schedule(self):
        from douyin_export_cleanup import cleanup_due
        self.assertFalse(legacy_managed(self.root))
        save_settings(self.service,self.config(enabled=False),now=self.now)
        self.assertTrue(legacy_managed(self.root))
        export_root=self.root/'douyin_inventory';export_root.mkdir()
        result=cleanup_due(SimpleNamespace(root=export_root),now=self.now,recycler=Mock())
        self.assertTrue(result['managed']);self.assertFalse((export_root/'export-cleanup.json').exists())
        self.assertIsNone(run_due(self.service,now=self.now+86400,recycler=self.recycle))

    def test_default_root_does_not_scan_other_department(self):
        with self.service.db() as db:db.execute("DELETE FROM meta WHERE key='daily_source_root'")
        self.assertEqual(preview(self.service,12,now=self.now)['roots'],[str(self.root.resolve())])

    def test_missed_runs_only_execute_once(self):
        save_settings(self.service,self.config(),now=self.now)
        later=self.now+40*86400
        self.assertEqual(run_due(self.service,now=later,recycler=self.recycle)['status'],'completed')
        self.assertGreater(settings(self.service)['state']['next_run'],later)
        self.assertIsNone(run_due(self.service,now=later,recycler=self.recycle))


if __name__=='__main__':unittest.main()
