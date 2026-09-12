import json
import tempfile
import unittest
from inventory_monitor import InventoryService


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = InventoryService(self.tmp.name)
        self.row = dict(sku='QCHXK0538', goods_id='88102', name='RTX 5070 Advanced OC', able=0, stock=0, purchase=0)
        self.s.accept_snapshot('catalog', [self.row], scope='account:2')
        self.details = dict(goods_id='88102', complete=True, depots=[
            dict(id='131', name='稀缺货源', stock=20, able=None, purchase=None),
            dict(id='125', name='样品库', stock=1, able=None, purchase=None)])
        self.product = dict(sku=self.row['sku'], goods_id='88102', name=self.row['name'], threshold=5,
                            stock_basis='warehouse_stock', warehouse_id='131', warehouse_name='稀缺货源')

    def verified(self):
        request = self.s.request_warehouses(self.row['sku'], '88102')
        self.s.accept_warehouses(request['request_id'], self.details, scope='account:2')

    def snap(self, ident, details=None, row=None):
        self.s.accept_snapshot(ident, [row or self.row], scope='account:2', journals={},
                               warehouse_details={self.row['sku']: self.details if details is None else details})

    def test_choice_is_real_per_product_and_persisted(self):
        with self.assertRaises(ValueError): self.s.save_product(self.product)
        self.verified()
        self.assertEqual([d['name'] for d in self.s.warehouse_options('88102')['depots']], ['稀缺货源', '样品库'])
        self.s.save_product(self.product)
        self.assertEqual(InventoryService(self.tmp.name).products()[0]['warehouse_id'], '131')
        with self.assertRaises(ValueError): self.s.save_product(dict(self.product, warehouse_id='999'))
        with self.assertRaises(ValueError): self.s.save_product(dict(self.product, warehouse_name='样品库'))

    def test_twenty_is_monitor_quantity_not_company_able_or_total_twenty_one(self):
        self.verified(); self.s.save_product(self.product); self.snap('first')
        p = self.s.status()['products'][0]
        self.assertEqual((p['monitor_qty'], p['able'], p['stock'], p['warehouse_stock']), (20, 0, 0, 20))
        self.assertEqual(p['risk'], 'normal')
        self.assertEqual(p['warehouse_name'], '稀缺货源')

    def test_missing_detail_does_not_fallback_to_company_stock_or_zero(self):
        self.verified(); self.s.save_product(self.product); self.snap('first')
        self.snap('failed', dict(goods_id='88102', complete=False, error='分库查询失败'))
        p = self.s.status()['products'][0]
        self.assertEqual(p['risk'], 'unknown')
        self.assertEqual(p['monitor_qty'], 20)
        self.assertEqual(self.s.status()['events'], [])

    def test_null_company_able_preserves_real_stock_and_warehouse_risk(self):
        self.verified(); self.s.save_product(self.product)
        self.snap('null', row=dict(self.row, able=None, stock=0, stock_unknown=True))
        p = self.s.status()['products'][0]
        self.assertIsNone(p['able']); self.assertEqual(p['monitor_qty'], 20)
        self.assertEqual(p['risk'], 'normal')

    def test_switch_cancels_old_alerts_and_never_compares_different_sources(self):
        self.s.save_product(dict(self.product, stock_basis='company_able'))
        self.s.accept_snapshot('old', [self.row], scope='account:2')
        self.verified(); self.s.save_product(self.product)
        self.assertIsNone(self.s.status()['products'][0]['monitor_qty'])
        self.snap('new')
        self.assertTrue(all(e['delivery_status'] == 'cancelled' for e in self.s.status()['events']))
        self.assertFalse(any(e['kind'] in ('recovery', 'stock_increase') for e in self.s.status()['events']))

    def test_negative_warehouse_is_not_oversold_and_company_ledger_is_not_used(self):
        self.verified(); self.s.save_product(self.product)
        self.details['depots'][0]['stock'] = -1
        self.snap('negative')
        p = self.s.status()['products'][0]
        self.assertEqual(p['risk'], 'negative_stock')
        events = self.s.status()['events']
        self.assertEqual(events[0]['kind'], 'negative_stock')
        self.assertEqual(events[0]['warehouse'], '稀缺货源')

    def test_bad_identity_isolated_and_regular_product_continues(self):
        self.verified(); self.s.save_product(self.product)
        self.s.save_product(dict(sku='B', name='正常商品', threshold=5))
        detail = dict(self.details, goods_id='999')
        self.s.accept_snapshot('mixed', [self.row, dict(sku='B', name='正常商品', able=2, stock=2, purchase=0)],
                               scope='account:2', journals={'B': []}, warehouse_details={self.row['sku']: detail})
        products = {p['sku']: p for p in self.s.status()['products']}
        self.assertEqual(products[self.row['sku']]['risk'], 'unknown')
        self.assertEqual(products['B']['risk'], 'low')

    def test_lookup_rejects_wrong_request_and_duplicate_warehouse_ids(self):
        req = self.s.request_warehouses(self.row['sku'], '88102')
        with self.assertRaises(ValueError): self.s.accept_warehouses('wrong', self.details, scope='account:2')
        duplicate = dict(self.details, depots=self.details['depots'] * 2)
        with self.assertRaises(ValueError): self.s.accept_warehouses(req['request_id'], duplicate, scope='account:2')


if __name__ == '__main__': unittest.main()
