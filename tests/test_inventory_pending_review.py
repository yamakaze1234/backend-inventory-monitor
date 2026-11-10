"""验证同仓待审扣减及库存变化触发，全部使用隔离模拟数据。"""
import unittest
import re
from sql_inventory_source import normalize
from test_inventory_sources import SourceTests, raw
from inventory_delivery import format_events, format_events_detailed


class PendingReviewTests(SourceTests):
    # 复用连接、轮询工具，不重复继承执行原测试集。
    def test_pending_unchanged_stock_decrease_triggers_low(self):
        self.connector.rows = [dict(raw(120), 分库可销数=63, 分库待审=44)]
        self.connect()
        self.service.save_product(dict(sku='SKU101', goods_id='101', name='测试商品', threshold=5))
        self.poll()
        self.connector.rows[0].update(分库数=103, 分库可销数=46)
        self.poll()
        status = self.service.status()
        product = status['products'][0]
        self.assertEqual((product['able'], product['risk']), (2, 'low'))
        event = next(e for e in status['events'] if e['kind'] == 'low')
        self.assertEqual(event['after']['pending_review'], 44)
        self.assertEqual(event['after']['erp_able'], 46)
        body = re.sub(r'<[^>]+>', '', format_events([event], self.service))
        self.assertIn('实际可销 2（较上次 -17）｜ERP 分库可销 46 − 待审 44', body)
        self.assertIn('分库待入：无（0）', body)
        self.assertLess(body.index('实际可销'), body.index('实物库存'))
        self.assertNotIn('= 实际可销', body)
        # 相同观察不重复提醒，待审增长导致负数仍须触发超售。
        count = len(status['events'])
        self.poll()
        self.assertEqual(len(self.service.status()['events']), count)
        self.connector.rows[0]['分库待审'] = 47
        self.poll()
        self.assertEqual(self.service.status()['products'][0]['risk'], 'oversold')

    def test_company_pending_does_not_borrow_other_warehouse(self):
        rows, _ = normalize([dict(raw(103), 分库可销数=46, 分库待审=44),
                             dict(raw(100), 库房='样品库', 分库待审=99)])
        self.assertEqual(rows[0]['able'], 2)

    def test_missing_pending_rejected_and_null_means_no_pending(self):
        value = raw()
        del value['分库待审']
        with self.assertRaises(ValueError):
            normalize([value])
        rows, _ = normalize([dict(raw(), 分库待审=None)])
        self.assertEqual(rows[0]['pending_review'], 0)
        self.assertEqual(rows[0]['able'], 8)
        # 待审空值不影响无订单商品，但可销空值仍为未知。
        rows, _ = normalize([dict(raw(), 分库可销数=None, 分库待审=None)])
        self.assertTrue(rows[0]['stock_unknown'])
        self.assertIsNone(rows[0]['able'])

    def test_pending_presence_is_explicit_in_compact_and_detailed_messages(self):
        event = dict(
            kind='low', sku='S', name='测试商品', threshold=5, observed_at=1700000000,
            before=dict(able=4, stock=65, purchase=0),
            after=dict(able=3, erp_able=55, pending_review=52, stock=67, purchase=3))
        compact_raw = format_events([event], self.service, now=1700000001)
        compact = re.sub(r'<[^>]+>', '', compact_raw)
        detailed = format_events_detailed([event], self.service, now=1700000001)
        expected = '分库待入：有，3（较上次 +3）'
        self.assertIn(expected, compact)
        self.assertIn(f'**<font color="#AD5700">{expected}</font>**', compact_raw)
        self.assertIn(f'**{expected}**', detailed)
        self.assertIn('实际可销 3（较上次 -1）｜ERP 分库可销 55 − 待审 52', compact)
        self.assertIn('**实际可销 3（较上次 -1）｜ERP 分库可销 55 − 待审 52**', detailed)

    def test_stock_basis_keeps_physical_stock(self):
        from sql_inventory_source import warehouse_key
        self.connector.rows = [dict(raw(103), 分库可销数=46, 分库待审=44)]
        self.connect()
        self.watch(stock_basis='warehouse_stock', warehouse_id=warehouse_key('公司大库'), warehouse_name='公司大库')
        self.poll()
        self.assertEqual(self.service.status()['products'][0]['monitor_qty'], 103)


# 只保留本模块新增的用例；连接工具仍从既有隔离测试基类继承。
def load_tests(loader, tests, pattern):
    return unittest.TestSuite(PendingReviewTests(name) for name in PendingReviewTests.__dict__ if name.startswith('test_'))
