"""多店并发与全局检查回归：使用事件控制真实线程，不连接浏览器、ERP 或群。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import threading
import time
import unittest

from douyin_export import ChromeExportWorker
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from inventory_panel import InventoryPanel


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.02)
    if not predicate():
        raise AssertionError('后台任务未在期限内完成')


class MultiShopTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'multi-shop-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.runtime = DouyinRuntime(SimpleNamespace(root=self.root), live=False)
        self.store = self.runtime.store

    def tearDown(self):
        # 测试目录留给统一回收站清理，不在测试中永久删除文件。
        self.runtime.close()

    def shops(self, count=3):
        result = []
        for index in range(count):
            ident = self.store.add_store('店铺' + str(index))
            self.store.save_browser(ident, 'chrome', 'profile-' + str(index))
            self.store.rebase(ident, confirmed=True)
            result.append(ident)
        return result

    def test_three_shops_really_overlap_and_fourth_waits(self):
        ids = self.shops(4)
        entered, finished = [], []
        release = threading.Event()
        lock = threading.Lock()
        def collect(ident):
            with lock:
                entered.append(ident)
            release.wait(4)
            with lock:
                finished.append(ident)
        with patch.object(self.runtime, 'collect', side_effect=collect):
            result = self.runtime.check_all()
            self.assertEqual(result['queued'], 4)
            wait_for(lambda: len(entered) == 3)
            self.assertEqual(self.runtime.check_all()['busy'], 4)
            self.assertEqual(len(entered), 3)
            release.set()
            wait_for(lambda: len(finished) == 4)
        self.assertEqual(set(finished), set(ids))
        self.assertTrue(all(not s['enabled'] for s in self.store.stores()))
        self.assertFalse(self.runtime.notification_ready())

    def test_one_failure_does_not_stop_other_shops_or_lock_retry(self):
        ids = self.shops()
        done = []
        def collect(ident):
            done.append(ident)
            if ident == ids[0]:
                raise ValueError('本店登录失效')
        with patch.object(self.runtime, 'collect', side_effect=collect):
            self.runtime.check_all()
            wait_for(lambda: len(done) == 3 and not self.runtime.pending_collections)
            self.assertEqual(self.store.store(ids[0])['status'], '本店登录失效')
            self.assertNotEqual(self.store.store(ids[1])['status'], '本店登录失效')
            self.assertTrue(self.runtime.submit('check', ids[0]))
            wait_for(lambda: len(done) == 4 and not self.runtime.pending_collections)
        self.assertGreater(self.runtime.due[ids[0]], time.monotonic())

    def test_delete_waits_for_own_collection_and_other_store_continues(self):
        ids = self.shops(2)
        entered, release, other = threading.Event(), threading.Event(), threading.Event()
        def collect(ident):
            if ident == ids[0]:
                entered.set(); release.wait(4)
            else:
                other.set()
        with patch.object(self.runtime, 'collect', side_effect=collect):
            self.runtime.submit('check', ids[0])
            self.assertTrue(entered.wait(2))
            reply = self.runtime.submit_remove(ids[0])
            self.runtime.submit('check', ids[1])
            self.assertTrue(other.wait(2))
            self.assertTrue(reply.empty())
            release.set()
            self.assertTrue(reply.get(timeout=3)['ok'])
        self.assertEqual([s['id'] for s in self.store.stores()], [ids[1]])

    def test_defaults_custom_names_and_deleted_shop_not_recreated(self):
        self.store.ensure_default_stores()
        self.assertEqual(len(self.store.stores()), 3)
        removed = self.store.stores()[1]['id']
        self.store.remove_store(removed)
        self.store.ensure_default_stores()
        self.store.add_store('自定义四店')
        self.assertEqual(len(self.store.stores()), 3)
        with self.assertRaises(ValueError):
            self.store.add_store('自定义四店')
        reopened = DouyinInventory(self.root)
        reopened.ensure_default_stores()
        self.assertNotIn(removed, [s['id'] for s in reopened.stores()])

    def test_removed_store_rejects_late_inventory(self):
        ident = self.shops(1)[0]
        revision = self.store.store(ident)['revision']
        self.store.remove_store(ident)
        with self.assertRaises(ValueError):
            self.store.accept(ident, revision, [], complete=True)

    def test_removed_shop_cloud_cleanup_preserves_other_shops_and_manual_rows(self):
        import json
        from tests.test_douyin_online import FakeTable
        from douyin_online import FIELD_NAMES, sync_online
        first, second = self.shops(2)
        fields = {name:'f' + str(i) for i,name in enumerate(FIELD_NAMES)}
        binding = dict(base_id='offline',table_id='offline',url='https://example.test/table',profile='test:test',fields=fields)
        (self.store.root/'online-table.json').write_text(json.dumps(binding), encoding='utf-8')
        cloud = FakeTable()
        cloud.rows = [dict(recordId='own',cells={fields['关联键']:first + ':123:456'}),
                      dict(recordId='other',cells={fields['关联键']:second + ':123:456'}),
                      dict(recordId='manual',cells={fields['备注']:'人工备注'})]
        self.store.remove_store(first)
        result = sync_online(self.store, first, client=cloud)
        self.assertTrue(result['ok'])
        self.assertEqual(cloud.deletes, ['own'])
        self.assertEqual([r['recordId'] for r in cloud.rows], ['other','manual'])

    def test_profiles_cannot_be_shared_and_unready_stores_are_skipped(self):
        first, second = self.shops(2)
        with self.assertRaises(ValueError):
            self.store.save_browser(second, 'edge', 'profile-0')
        self.store.save_browser(second, 'chrome', '')
        self.store.rebase(second, confirmed=True)
        with patch.object(self.runtime, 'collect'):
            result = self.runtime.check_all()
            self.assertEqual(result['queued'], 1)
            self.assertIn('独立连接标识', result['skipped'][0])
            wait_for(lambda: not self.runtime.pending_collections)

    def test_both_inventory_requests_are_independent(self):
        service, runtime, notice = Mock(), Mock(), Mock()
        runtime.check_all.return_value = dict(queued=3, busy=0, skipped=[])
        panel = SimpleNamespace(service=service, ensure_douyin_runtime=lambda: runtime, notice=notice, refresh=Mock())
        service.request_check.side_effect = RuntimeError('ERP 不可用')
        InventoryPanel.check(panel)
        runtime.check_all.assert_called_once()
        self.assertIn('ERP 库存：提交失败', notice.set.call_args.args[0])
        self.assertIn('已排队 3 店', notice.set.call_args.args[0])
        service.request_check.side_effect = None
        runtime.check_all.side_effect = RuntimeError('抖店不可用')
        InventoryPanel.check(panel)
        self.assertEqual(service.request_check.call_count, 2)
        self.assertIn('抖店库存：提交失败', notice.set.call_args.args[0])

    def test_export_waits_overlap_but_download_claims_are_serial(self):
        # 真正进入导出器的锁路径：旧全局锁无法通过三方屏障。
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active, maximum = [0], [0]
        root = self.root
        class Exporter(ChromeExportWorker):
            def __init__(self, index):
                self.root, self.ident, self.page = root, str(index), 'page-' + str(index)
                self.expected_shop, self.browser_profile = '店铺' + str(index), 'connection-' + str(index)
            def request_export(self):
                barrier.wait(timeout=3)
                return dict(shop=self.expected_shop, started=time.time(), count=1, old=[])
            def refresh_reports(self):
                pass
            def records(self):
                from datetime import datetime
                return [dict(key='1', time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), ready=True)]
            def download_report(self, *args):
                with lock:
                    active[0] += 1; maximum[0] = max(maximum[0], active[0])
                time.sleep(.05)
                with lock:
                    active[0] -= 1
                return dict(ok=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda i: Exporter(i).call('collect'), range(3)))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(maximum[0], 1)


if __name__ == '__main__':
    unittest.main()
