import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from douyin_inventory import DouyinInventory, stock_number
from douyin_inventory_worker import collect_all
from douyin_inventory_runtime import worker_command


class DouyinInventoryTest(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'douyin-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))
        self.store = DouyinInventory(self.root)
        self.ident = self.store.add_store('测试店铺', 'unoccupied_stock')
        self.store.rebase(self.ident, confirmed=True)
        self.accept(8)
        self.store.enable(self.ident, True)

    def accept(self, quantity, *, ident=None, products=None, complete=True):
        ident = ident or self.ident
        rows = products if products is not None else [dict(product_id='p1', product_name='测试商品',
                    skus=[dict(sku_id='s1', sku_name='测试规格', unoccupied_stock=quantity)])]
        return self.store.accept(ident, self.store.store(ident)['revision'], rows, complete=complete)

    def test_baseline_zero_and_repeated_zero_are_silent(self):
        self.assertEqual(self.accept(0), [])
        self.assertEqual(self.accept(0), [])

    def test_restock_rearms_and_restart_preserves_baseline(self):
        self.accept(5)
        self.assertEqual(len(self.accept(0)), 1)
        self.store = DouyinInventory(self.root)
        self.assertEqual(self.accept(0), [])
        self.accept(7)
        self.assertEqual(len(self.accept(0)), 1)

    def test_missing_quantity_is_unknown_and_breaks_continuity(self):
        self.accept(5)
        self.assertEqual(self.accept(None), [])
        self.assertIsNone(self.store.snapshots(self.ident)[0]['quantity'])
        self.assertEqual(self.accept(0), [])

    def test_missing_rows_do_not_mean_zero(self):
        self.accept(5)
        self.accept(None, products=[])
        self.assertEqual(self.accept(0), [])

    def test_partial_query_never_changes_baseline(self):
        self.accept(5)
        with self.assertRaises(ValueError):
            self.accept(0, complete=False)
        self.assertEqual(self.store.snapshots(self.ident)[0]['quantity'], 5)

    def test_configuration_revision_rejects_inflight_snapshot(self):
        revision = self.store.store(self.ident)['revision']
        self.store.enable(self.ident, False)
        with self.assertRaises(ValueError):
            self.store.accept(self.ident, revision, [], complete=True)

    def test_other_shop_cannot_trigger_original_shop(self):
        self.accept(5)
        other = self.store.add_store('另一测试店铺', 'unoccupied_stock')
        self.store.rebase(other, confirmed=True)
        self.accept(0, ident=other)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.snapshots(self.ident)[0]['quantity'], 5)

    def test_pause_cancels_pending_and_resume_rebases(self):
        self.accept(5)
        self.accept(0)
        self.store.enable(self.ident, False)
        sender = Mock()
        self.store.deliver(sender)
        sender.assert_not_called()
        self.store.enable(self.ident, True)
        self.assertEqual(self.accept(0), [])
        self.assertEqual(self.store.events()[0]['status'], 'preview')

    def test_no_delivery_even_when_notify_defaults_true(self):
        """误用旧默认参数也只能记录，不能进入机器人发送。"""
        self.accept(5)
        self.accept(0)
        sender = Mock(side_effect=[{'status': 'failed', 'retryable': True}, {'status': 'sent'}])
        self.store.deliver(sender, now=1000)
        self.store.deliver(sender, now=1001)
        sender.assert_not_called()
        self.store.deliver(sender, now=1061)
        self.store.deliver(sender, now=2000)
        sender.assert_not_called()
        self.assertEqual(self.store.events()[0]['status'], 'preview')

    def test_legacy_pending_jobs_cancelled_on_upgrade(self):
        """旧版本积压记录不得在本次升级后发送。"""
        self.accept(5)
        self.accept(0)
        with self.store.db() as db:
            db.execute("UPDATE events SET status='pending'")
        self.store = DouyinInventory(self.root)
        sender = Mock(side_effect=TimeoutError)
        self.store.deliver(sender)
        self.store.deliver(sender)
        sender.assert_not_called()
        self.assertEqual(self.store.events()[0]['status'], 'cancelled')

    def test_invalid_numbers_never_become_zero(self):
        for value in (None, True, False, '', 'NaN', -1, 1.2, {}, '０'):
            self.assertIsNone(stock_number(value))
        self.assertEqual(stock_number('0'), 0)

    def test_preview_events_never_enter_live_delivery(self):
        self.accept(5)
        revision = self.store.store(self.ident)['revision']
        self.store.accept(self.ident, revision, [dict(product_id='p1', skus=[dict(sku_id='s1', unoccupied_stock=0)])], complete=True, notify=False)
        sender = Mock()
        self.store.deliver(sender)
        sender.assert_not_called()
        self.assertEqual(self.store.events()[0]['status'], 'preview')

    def test_duplicate_skus_rejected_atomically(self):
        self.accept(5)
        with self.assertRaises(ValueError):
            self.accept(None, products=[dict(product_id='p1', skus=[dict(sku_id='s1'), dict(sku_id='s1')])])
        self.assertEqual(self.store.snapshots(self.ident)[0]['quantity'], 5)


class PaginationTest(unittest.TestCase):
    def client(self, pages):
        client = Mock()
        client.is_logged_in.return_value = True
        client._list_page_raw.side_effect = [dict(status=200, resp_body=p) for p in pages]
        client._parse_response.side_effect = lambda body: body['data']
        return client

    def test_complete_pages(self):
        pages = [dict(code=0, total=2, data=[dict(product_id=str(i), skus=[])]) for i in (1, 2)]
        self.assertEqual(len(collect_all(self.client(pages))), 2)

    def test_repeated_page_rejected(self):
        page = dict(code=0, total=2, data=[dict(product_id='1', skus=[])])
        with self.assertRaises(ValueError):
            collect_all(self.client([page, page]))

    def test_missing_total_and_missing_code_rejected(self):
        for body in [dict(code=0, data=[]), dict(total=0, data=[])]:
            with self.assertRaises(ValueError):
                collect_all(self.client([body]))

    def test_empty_early_page_rejected(self):
        with self.assertRaises(ValueError):
            collect_all(self.client([dict(code=0, total=1, data=[])]))


class WorkerPackageTest(unittest.TestCase):
    def test_missing_client_worker_and_source_environment_have_distinct_errors(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'douyin-tests'
        base.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=base))
        with self.assertRaisesRegex(ValueError, 'stock_client.py'):
            worker_command(root, root / 'profile')
        client = root / 'app' / 'stock_client.py'
        client.parent.mkdir()
        client.write_text('', encoding='utf-8')
        with patch('douyin_inventory_runtime.sys.frozen', True, create=True), \
                patch('douyin_inventory_runtime.sys.executable', str(root / 'main.exe')):
            with self.assertRaisesRegex(ValueError, 'worker.exe'):
                worker_command(root, root / 'profile')
            (root / 'douyin_inventory_worker.exe').write_bytes(b'worker')
            self.assertEqual(worker_command(root, root / 'profile')[0], str(root / 'douyin_inventory_worker.exe'))
        with patch('douyin_inventory_runtime.sys.frozen', False, create=True):
            with self.assertRaisesRegex(ValueError, 'Python 环境'):
                worker_command(root, root / 'profile')


if __name__ == '__main__':
    unittest.main()
