"""勾选范围、首次身份核验和专用浏览器自动启动回归；不连接真实店铺。"""
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from douyin_export import ChromeMachineExportWorker, PRODUCT_URL
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_managed_export import ManagedExportWorker


class PatrolTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'patrol-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.store = DouyinInventory(self.root)
        self.ids = [self.store.add_store(name) for name in ('技嘉旗舰店', '英特尔官方旗舰店', '乔思伯官方旗舰店')]
        # 无调度线程的运行时用来逐步检查排队边界，真实三店并发另有线程回归覆盖。
        self.runtime = object.__new__(DouyinRuntime)
        self.runtime.store = self.store
        self.runtime.commands = queue.Queue()
        self.runtime.collection_lock = threading.Lock()
        self.runtime.pending_collections = set()
        self.runtime.due = {}
        self.runtime.worker = Mock(return_value=Mock())
        self.runtime.collect = Mock()

    def test_pending_report_retries_without_confirming_again(self):
        from douyin_export import ReportPending
        ident = self.ids[0]
        self.runtime.stop_event = Mock()
        self.runtime.stop_event.wait.return_value = False
        self.runtime.collect.side_effect = [ReportPending('等待'), None]
        self.runtime.collect_reserved(ident)
        self.assertEqual(self.runtime.collect.call_count, 2)
        self.runtime.stop_event.wait.assert_called_once_with(30)
        self.runtime.worker.assert_not_called()
        self.assertNotIn(ident, self.runtime.report_waiting)

    def test_pending_report_stops_when_unselected(self):
        from douyin_export import ReportPending
        ident = self.ids[0]
        self.runtime.stop_event = Mock()
        self.runtime.stop_event.wait.side_effect = lambda _: self.store.select_patrol(ident, False) and False
        self.runtime.collect.side_effect = ReportPending('等待')
        self.runtime.collect_reserved(ident)
        self.runtime.collect.assert_called_once()
        self.assertNotIn(ident, self.runtime.pending_collections)

    def test_selection_persists_and_only_selected_shops_are_queued(self):
        self.store.select_patrol(self.ids[1], False)
        self.runtime.store = DouyinInventory(self.root)
        result = self.runtime.check_all()
        self.assertEqual(result, dict(queued=2, busy=0, skipped=[]))
        jobs = [self.runtime.commands.get_nowait() for _ in range(2)]
        self.assertEqual(jobs, [('inspect', self.ids[0]), ('inspect', self.ids[2])])
        self.assertEqual(self.runtime.check_all()['busy'], 2)
        self.assertFalse(self.runtime.store.store(self.ids[1])['patrol_selected'])

    def test_empty_selection_stays_empty_after_restart(self):
        for ident in self.ids:
            self.store.select_patrol(ident, False)
        self.runtime.store = DouyinInventory(self.root)
        self.assertEqual(self.runtime.check_all()['queued'], 0)
        self.assertTrue(self.runtime.commands.empty())
        self.store.remove_store(self.ids[0])
        with self.assertRaises(ValueError):
            self.store.select_patrol(self.ids[0], True)

    def test_uncheck_preserves_snapshots_and_removes_shop_from_future_schedule(self):
        for ident in self.ids:
            self.store.rebase(ident, confirmed=True)
            self.store.accept(ident, self.store.store(ident)['revision'], [], complete=True)
            self.store.enable(ident, True)
        before = self.store.store(self.ids[1])
        self.store.select_patrol(self.ids[1], False)
        self.assertEqual([s['id'] for s in self.runtime.due_stores()], [self.ids[0], self.ids[2]])
        after = self.store.store(self.ids[1])
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual(before['checked'], after['checked'])
        self.assertEqual(before['enabled'], after['enabled'])

    def test_unchecked_queued_job_never_opens_browser_and_releases_occupancy(self):
        ident = self.ids[0]
        self.runtime.submit('inspect', ident)
        self.store.select_patrol(ident, False)
        self.runtime.execute(*self.runtime.commands.get_nowait())
        self.runtime.worker.assert_not_called()
        self.runtime.collect.assert_not_called()
        self.assertNotIn(ident, self.runtime.pending_collections)

    def test_first_inspection_verifies_identity_before_confirming_and_collecting(self):
        ident = self.ids[0]
        def login(operation):
            self.assertEqual(operation, 'login')
            self.assertFalse(self.store.store(ident)['confirmed'])
        self.runtime.worker.return_value.call.side_effect = login
        self.runtime.collect.side_effect = lambda key: self.assertTrue(self.store.store(key)['confirmed'])
        self.runtime.reserve_collection(ident)
        self.runtime.execute('inspect', ident)
        self.runtime.collect.assert_called_once_with(ident)
        self.assertNotIn(ident, self.runtime.pending_collections)

    def test_wrong_shop_keeps_existing_baseline_and_does_not_collect(self):
        ident = self.ids[0]
        self.store.rebase(ident, confirmed=True)
        self.store.accept(ident, self.store.store(ident)['revision'],
            [dict(product_id='123', skus=[dict(sku_id='456', export_sellable=8)])], complete=True)
        with self.store.db() as db:
            db.execute('UPDATE stores SET confirmed=0 WHERE id=?', (ident,))
        before = self.store.snapshots(ident)
        self.runtime.worker.return_value.call.side_effect = ValueError('店铺不一致')
        self.runtime.reserve_collection(ident)
        with self.assertRaisesRegex(ValueError, '店铺不一致'):
            self.runtime.execute('inspect', ident)
        self.runtime.collect.assert_not_called()
        self.assertEqual(self.store.snapshots(ident), before)
        self.assertFalse(self.store.store(ident)['confirmed'])
        self.assertNotIn(ident, self.runtime.pending_collections)

    def test_batch_timer_targets_selection_and_reports_missing_baselines(self):
        self.store.rebase(self.ids[0], confirmed=True)
        self.store.accept(self.ids[0], self.store.store(self.ids[0])['revision'], [], complete=True)
        self.store.select_patrol(self.ids[2], False)
        result = self.runtime.toggle_selected()
        self.assertEqual(result['queued'], 1)
        self.assertTrue(result['enabled'])
        self.assertIn('英特尔官方旗舰店', result['skipped'][0])
        self.assertEqual(self.runtime.commands.get_nowait(), ('enable', self.ids[0]))

    def test_closed_managed_browser_opens_for_login_and_collection(self):
        for operation in ('login', 'collect'):
            with self.subTest(operation=operation):
                worker = ManagedExportWorker(None, self.store.root / 'profiles' / self.ids[0])
                worker._browser = Mock()
                worker._browser.running.return_value = None
                worker.open_page = Mock()
                with patch.object(ChromeMachineExportWorker, 'call', return_value=dict(ok=True)):
                    self.assertTrue(worker.call(operation)['ok'])
                worker._browser.open.assert_called_once_with(PRODUCT_URL)
        worker._browser.open.reset_mock()
        worker._browser.running.return_value = 'owned-browser'
        with patch.object(ChromeMachineExportWorker, 'call', return_value=dict(ok=True)):
            worker.call('collect')
        worker._browser.open.assert_not_called()

    def test_mixed_ready_and_unready_selection_can_pause_running_shop(self):
        ident = self.ids[0]
        self.store.rebase(ident, confirmed=True)
        self.store.accept(ident, self.store.store(ident)['revision'], [], complete=True)
        self.store.enable(ident, True)
        result = self.runtime.toggle_selected()
        self.assertFalse(result['enabled'])
        self.assertEqual(result['skipped'], [])
        self.assertEqual(self.runtime.commands.get_nowait(), ('pause', ident))


if __name__ == '__main__':
    unittest.main()
