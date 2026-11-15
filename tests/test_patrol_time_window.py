"""各计划独立时段与开闭边界；只使用临时配置。"""
from datetime import datetime
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from inventory_patrol_window import allowed, load, save
from inventory_schedule_view import window_form
from inventory_monitor import InventoryService
from inventory_sql_service import SqlWorker


def at(clock):
    return datetime.fromisoformat('2026-11-15T'+clock+':00+08:00').timestamp()


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_default_all_day_and_independent_stores(self):
        self.assertTrue(allowed(self.root,'inventory',at('03:00')))
        save(self.root,'douyin:a',False,'09:00','18:00')
        self.assertFalse(allowed(self.root,'douyin:a',at('03:00')))
        self.assertTrue(allowed(self.root,'douyin:b',at('03:00')))

    def test_day_and_midnight_boundaries(self):
        for start,end,yes,no in [('09:00','18:00',['09:00','17:59'],['08:59','18:00']),
                                ('22:00','02:00',['22:00','23:59','00:00','01:59'],['21:59','02:00'])]:
            save(self.root,'shipping',False,start,end)
            for clock in yes:self.assertTrue(allowed(self.root,'shipping',at(clock)))
            for clock in no:self.assertFalse(allowed(self.root,'shipping',at(clock)))

    def test_invalid_save_keeps_old_and_corruption_fails_closed(self):
        save(self.root,'price',False,'09:00','18:00')
        for start,end in [('bad','18:00'),('09:00','09:00')]:
            with self.assertRaises(ValueError):save(self.root,'price',False,start,end)
        self.assertEqual(load(self.root,'price')['start'],'09:00')
        with closing(sqlite3.connect(self.root/'patrol-windows.sqlite3')) as db, db:
            db.execute("UPDATE windows SET data='broken' WHERE key='price'")
        self.assertFalse(allowed(self.root,'price',at('12:00')))
        save(self.root,'price',True)
        self.assertTrue(allowed(self.root,'price',at('03:00')))

    def test_ui_exposes_full_day_and_independent_key(self):
        form=window_form(self.root,'douyin:abc')
        self.assertTrue(form['values']['allDay'])
        self.assertEqual(form['values']['key'],'douyin:abc')
        self.assertEqual([f['key'] for f in form['fields']],['allDay','start','end'])

    def test_queued_shop_rechecks_window_but_manual_bypasses(self):
        from douyin_inventory import DouyinInventory
        from douyin_inventory_runtime import DouyinRuntime
        runtime=object.__new__(DouyinRuntime)
        runtime.store=DouyinInventory(self.root)
        ident=runtime.store.add_store('技嘉旗舰店')
        runtime.stop_event=threading.Event();runtime.collection_lock=threading.Lock()
        runtime.pending_collections={ident};runtime.execute=Mock()
        with patch('inventory_patrol_window.allowed',return_value=False):
            runtime.execute_job('scheduled-inspect',ident)
            runtime.execute.assert_not_called()
            self.assertNotIn(ident,runtime.pending_collections)
            runtime.execute_job('inspect',ident)
        runtime.execute.assert_called_once_with('inspect',ident)

    def test_sql_closed_window_does_not_query_but_manual_can_run(self):
        service=InventoryService(self.root);worker=SqlWorker(service)
        source=Mock(scope='fixture',legacy_text=False);worker.source=source
        service.pending_warehouse_request=Mock(return_value=None)
        save(self.root,'inventory',False,'09:00','18:00')
        worker.step(at('03:00'))
        source.read_bundle.assert_not_called()
        service.request_check()
        # 无商品的手动请求仍会被领取并消费，不受时段拦截。
        worker.step(at('03:00'))
        with service.db() as db:self.assertFalse(service.get(db,'manual_check_requested'))
