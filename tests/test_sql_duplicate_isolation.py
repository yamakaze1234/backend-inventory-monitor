import copy
import tempfile
import time
import unittest

from inventory_sources import InventoryRuntimeService
from inventory_panel import product_risk_label
from tests.test_inventory_sources import Connector, SETTINGS, raw


class DuplicateIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = InventoryRuntimeService(self.temp.name)
        self.c = Connector()
        self.now = time.time()
        self.s.configure_sql(SETTINGS, 'fixture-only', connector=self.c)
        self.w = self.s.sql_worker
        self.w.step(self.now)
        self.s.save_product(dict(sku='SKU101', goods_id='101', name='测试商品', threshold=0))
        self.poll()

    def poll(self):
        self.s.request_check()
        self.now += 31
        self.w.step(self.now)

    def duplicate_other(self):
        bad = dict(raw(), goods_id='55707', 商品编码='BAD')
        self.c.rows.extend([bad, copy.deepcopy(bad)])
        self.c.catalog = [dict(goods_id='101', 商品编码='SKU101', 商品名称='测试商品', 总数量=8, 可销数=8, 待入=0),
                          dict(goods_id='55707', 商品编码='BAD', 商品名称='重复商品', 总数量=1, 可销数=1, 待入=0)]

    def test_unwatched_duplicate_does_not_block_good_stock(self):
        self.duplicate_other()
        self.c.rows[0].update(分库数=12, 分库可销数=12)
        self.w.refresh_catalog()
        self.poll()
        status = self.s.status()
        self.assertEqual(status['connection'], 'online')
        self.assertEqual(status['products'][0]['able'], 12)
        self.assertIn('55707', status['data_issues'])
        self.assertFalse(self.w.refresh_requested)
        self.assertEqual(self.w.failures, 0)
        self.assertFalse(self.s.catalog('55707', mode='goods_id'))

    def test_watched_duplicate_preserves_baseline_without_false_events_and_recovers(self):
        self.c.rows *= 2
        self.poll()
        status = self.s.status()
        self.assertEqual(status['connection'], 'online')
        self.assertIsNone(status['products'][0]['able'])
        self.assertEqual(product_risk_label(status['products'][0], 'online')[0], '数据异常（已隔离）')
        self.assertFalse(status['events'])
        with self.s.db() as db:
            self.assertEqual(db.execute('select count(*) from snapshots').fetchone()[0], 1)
        self.c.rows = [raw(11)]
        self.poll()
        self.assertFalse(self.s.status()['data_issues'])
        self.assertEqual(self.s.status()['products'][0]['able'], 11)
        self.assertEqual(len(self.s.status()['events']), 1)

    def test_reconnect_refresh_preserves_pause_and_no_new_snapshot_or_event(self):
        self.s.set_enabled(False)
        before = self.s.status()['checked_at']
        self.c.rows = [raw(99, 10)]
        self.w.configure(self.w.source)
        self.now += 31
        self.w.step(self.now)
        status = self.s.status()
        self.assertFalse(status['enabled'])
        self.assertEqual(status['checked_at'], before)
        self.assertEqual(status['products'][0]['able'], 8)
        self.assertFalse(status['events'])

    def test_explicit_exclusion_filters_catalog_and_blocks_add(self):
        self.duplicate_other()
        with self.s.db() as db:
            self.s.put(db, 'sql_excluded_goods_ids', ['55707'])
        self.w.refresh_catalog()
        self.poll()
        self.assertFalse(self.s.status()['data_issues'])
        self.assertFalse(self.s.catalog('55707', mode='goods_id'))
        with self.assertRaisesRegex(ValueError, '已排除'):
            self.s.save_product(dict(sku='BAD', goods_id='55707', name='重复商品', threshold=0))

    def test_query_and_data_failures_have_distinct_kinds(self):
        self.c.fail = True
        self.poll()
        self.assertEqual(self.s.status()['error_kind'], 'query')
        self.c.fail = False
        # 表结构缺失属于全局失败；单商品的空编码改由隔离测试覆盖。
        del self.c.rows[0]['商品编码']
        self.now += 61
        self.poll()
        self.assertEqual(self.s.status()['error_kind'], 'data')


if __name__ == '__main__':
    unittest.main()
