import tempfile
import unittest
from pathlib import Path

from inventory_monitor import InventoryService
from inventory_rules import default_threshold, classify


class InventoryTests(unittest.TestCase):
    def test_low_stock_reaching_zero_warns_once_and_rearms(self):
        self.snap('zero-a', 2)
        self.snap('zero-b', 0)
        self.snap('zero-c', 0)
        kinds=[e['kind'] for e in self.s.status()['events']]
        self.assertEqual(kinds.count('out_of_stock'),1)
        self.snap('zero-d', 2)
        self.snap('zero-e', 0)
        self.assertEqual(sum(e['kind']=='out_of_stock' for e in self.s.status()['events']),2)
        self.snap('zero-f', -1)
        self.assertEqual(self.s.status()['events'][0]['kind'],'oversold')

    def test_explicit_unknown_preserves_catalog_without_alerting(self):
        self.snap('before-unknown', 30)
        rows=[dict(sku='A',goods_id='436535',name='内存',able=None,purchase=None,stock=None,stock_unknown=True),
              dict(sku='B',name='正常商品',able=20,purchase=0,stock=20)]
        self.s.accept_snapshot('unknown',rows,scope='test-account:2',journals={})
        self.assertEqual(len(self.s.catalog()),2)
        self.assertIsNone(self.s.catalog('436535',mode='goods_id')[0]['able'])
        self.assertEqual(self.s.status()['products'][0]['risk'],'unknown')
        self.assertEqual(self.s.status()['events'],[])
        self.snap('after-unknown',29)
        self.assertEqual(self.s.status()['products'][0]['risk'],'normal')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = InventoryService(Path(self.temp.name))
        self.s.save_product(dict(sku='A', name='内存', category='内存', threshold=10))

    def snap(self, key, able, **kwargs):
        return self.s.accept_snapshot(key, [dict(sku='A', name='内存', able=able, purchase=0, stock=30)],
                                      scope='test-account:2', **kwargs)

    def test_thresholds_and_priority(self):
        self.assertEqual(default_threshold('显卡', 'RTX 5070 Ti'), 5)
        self.assertEqual(default_threshold('显卡', 'RTX 5060'), 20)
        self.assertEqual(default_threshold('显卡', 'RTX 4090'), 20)
        self.assertEqual(default_threshold('CPU'), 20)
        self.assertEqual(default_threshold('内存'), 10)
        self.assertEqual(classify(-1, 10), 'oversold')
        self.assertEqual(classify(10, 10), 'low')

    def test_catalog_search_covers_complete_catalog(self):
        rows = [dict(sku=f'CODE-{i}', name=f'品牌 DDR5 32G {i}', able=10, purchase=0, stock=10) for i in range(250)]
        self.s.accept_snapshot('catalog-search', rows, scope='test')
        self.assertEqual(len(self.s.catalog('ddr5 32g', limit=None)), 250)
        self.assertEqual(self.s.catalog('code-249')[0]['sku'], 'CODE-249')
        self.assertEqual(self.s.catalog('不存在'), [])

    def test_goodsid_search_and_saved_identity(self):
        self.s.accept_snapshot('goodsid', [dict(sku='SKU-88', goods_id='57339', name='测试内存 32G', able=10, purchase=0, stock=10)], scope='test')
        self.assertEqual(self.s.catalog('57339', mode='goods_id')[0]['sku'], 'SKU-88')
        self.assertEqual(self.s.catalog('573', mode='goods_id'), [])
        self.assertEqual(self.s.catalog('内存 32', mode='name')[0]['goods_id'], '57339')
        self.assertEqual(self.s.catalog('SKU-88', mode='name'), [])
        self.s.save_product(dict(sku='SKU-88', name='测试内存', category='内存', threshold=10))
        self.assertEqual(next(p for p in self.s.products() if p['sku'] == 'SKU-88')['goods_id'], '57339')
        with self.assertRaises(ValueError):
            self.s.save_product(dict(sku='SKU-88', goods_id='999', name='测试内存', threshold=10))

    def test_remove_watch_cancels_pending_but_preserves_catalog(self):
        from inventory_delivery import DeliveryWorker
        self.snap('remove-baseline', 3)
        result = self.s.remove_product('A')
        self.assertEqual(result['sku'], 'A')
        self.assertEqual(InventoryService(self.temp.name).products(), [])
        self.assertEqual(self.s.catalog()[0]['sku'], 'A')
        self.assertEqual(self.s.status()['events'][0]['delivery_status'], 'cancelled')
        calls = []
        DeliveryWorker(self.s, lambda body: calls.append(body)).run_once()
        self.assertEqual(calls, [])
        self.snap('after-removal', -2)
        self.assertEqual(len(self.s.status()['events']), 1)
        self.assertIsNone(self.s.remove_product('A'))

    def test_pending_inbound_only_on_first_positive_or_increase(self):
        for i, pending in enumerate((5, 5, 8, 3, 0, 2)):
            self.s.accept_snapshot(str(i), [dict(sku='A', name='内存', able=30, stock=30, purchase=pending)], scope='test')
        events = self.s.status()['events']
        self.assertEqual([e['kind'] for e in events], ['pending_inbound'] * 3)
        self.assertEqual([e['after']['purchase'] for e in events], [2, 8, 5])
        self.s.save_product(dict(sku='A', name='内存', threshold=10, alerts={'pending_inbound':False}))
        self.s.accept_snapshot('disabled', [dict(sku='A', name='内存', able=30, stock=30, purchase=10)], scope='test')
        self.assertEqual(len(self.s.status()['events']), 3)

    def test_manual_threshold_persists(self):
        self.s.save_product(dict(sku='A', name='内存', category='内存', threshold=33))
        self.assertEqual(InventoryService(self.temp.name).products()[0]['threshold'], 33)
        for invalid in (-1, 1.5, True, 'abc'):
            with self.assertRaises(ValueError):
                self.s.save_product(dict(sku='X', name='x', category='其他', threshold=invalid))

    def test_release_cancels_test_backlog_and_rechecks_once(self):
        from inventory_app import promote_to_v1
        self.snap('before-release', 2)
        self.s.set_interval(30)
        self.assertTrue(promote_to_v1(self.s))
        self.assertEqual(self.s.status()['events'][0]['delivery_status'], 'cancelled')
        self.assertEqual(self.s.status()['interval_seconds'], 1800)
        self.assertEqual(self.s.status()['next_check'], 0)
        self.snap('after-release', 2)
        self.assertEqual(self.s.status()['events'][0]['delivery_status'], 'pending')
        self.assertFalse(promote_to_v1(self.s))
        self.assertEqual(self.s.status()['events'][0]['delivery_status'], 'pending')

    def test_configurable_collection_interval(self):
        self.assertEqual(self.s.status()['interval_seconds'], 7200)
        self.s.set_interval('30')
        self.snap('interval', 30)
        status = InventoryService(self.temp.name).status()
        self.assertEqual(status['interval_seconds'], 1800)
        self.assertEqual(status['next_check'] - status['checked_at'], 1800)
        for value in ('0', '-1', '1.5', 'abc'):
            with self.assertRaises(ValueError):
                self.s.set_interval(value)

    def test_first_risk_dedup_and_recovery(self):
        self.snap('1', 10)
        self.snap('2', 8)
        self.snap('2', 8)
        self.snap('3', -1)
        self.snap('4', 3)
        self.snap('5', 21)
        self.assertEqual([e['kind'] for e in reversed(self.s.status()['events'])],
                         ['low', 'oversold', 'recovery', 'recovery'])

    def test_invalid_snapshot_does_not_replace_baseline(self):
        self.snap('1', 30)
        with self.assertRaises(ValueError):
            self.snap('2', 0, complete=False)
        with self.assertRaises(ValueError):
            self.snap('3', float('nan'))
        with self.assertRaises(ValueError):
            self.snap('4', 0, warehouse='另一个仓库')
        self.assertEqual(self.s.status()['products'][0]['able'], 30)

    def test_missing_is_unknown_not_zero(self):
        self.snap('1', 30)
        self.s.accept_snapshot('2', [], scope='test-account:2')
        self.assertEqual(self.s.status()['products'][0]['risk'], 'unknown')
        self.assertEqual(len(self.s.status()['events']), 0)

    def test_scope_change_requires_explicit_reset(self):
        self.snap('1', 30)
        with self.assertRaises(ValueError):
            self.s.accept_snapshot('2', [], scope='other:2')

    def test_schedule_persistence(self):
        self.snap('1', 30, observed_at=1000)
        self.assertEqual(InventoryService(self.temp.name).status()['next_check'], 8200)
        self.s.request_check()
        self.assertEqual(self.s.status()['next_check'], 0)

    def test_threshold_edit_identifies_cause(self):
        self.snap('1', 30)
        self.s.save_product(dict(sku='A', name='内存', category='内存', threshold=40))
        self.assertEqual(self.s.status()['events'][0]['cause'], 'threshold_changed')

    def test_real_stock_increase_is_not_inbound(self):
        self.snap('1', 30)
        self.s.accept_snapshot('2', [dict(sku='A', name='内存', able=40, purchase=0, stock=40)], scope='test-account:2')
        self.assertEqual(self.s.status()['events'][0]['kind'], 'stock_increase')

    def test_ambiguous_message_does_not_bind(self):
        self.s.save_product(dict(sku='A', name='内存', category='内存', threshold=10, keywords='同型号'))
        self.s.save_product(dict(sku='B', name='内存二', category='内存', threshold=10, keywords='同型号'))
        self.s.record_message(dict(message_id='msg', text='同型号到货', sender='经理', created_at=1000))
        self.assertEqual(self.s.related_messages('A', now=1001), [])

    def test_threshold_edit_while_stale_alerts_on_next_fresh_check(self):
        self.snap('1', 30, observed_at=1000)
        self.s.save_product(dict(sku='A',name='内存',category='内存',threshold=40))
        self.snap('2', 30)
        self.assertEqual(self.s.status()['events'][0]['kind'], 'low')
        self.assertEqual(self.s.status()['events'][0]['cause'], 'threshold_changed')
        self.snap('3', 30)
        self.assertEqual(len(self.s.status()['events']), 1)

    def test_sku_prefix_is_not_product_identity(self):
        self.s.record_message(dict(message_id='msg',text='A123 到货',created_at=1000))
        self.assertEqual(self.s.related_messages('A',now=1001), [])

    def test_configured_name_is_not_overwritten_by_snapshot(self):
        self.s.save_product(dict(sku='A',name='我的内存',category='内存',threshold=10))
        self.snap('1',30)
        self.assertEqual(self.s.status()['products'][0]['name'],'我的内存')

    def test_formatted_message_time_preserved(self):
        self.s.record_message(dict(message_id='iso',text='A 到货',created_at='2026-09-09 10:00:00'))
        from datetime import datetime, timezone, timedelta
        moment=datetime(2026,9,9,10,tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.assertEqual(self.s.related_messages('A',now=moment+1)[0]['created_at'],moment)


if __name__ == '__main__':
    unittest.main()
