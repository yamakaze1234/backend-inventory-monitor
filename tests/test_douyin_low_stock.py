"""平台低库存阈值、配件例外和持久化边界，不连接真实服务。"""
import tempfile
from pathlib import Path
import unittest
from douyin_low_stock import validate, threshold, is_low
from douyin_inventory import DouyinInventory
from douyin_scope import MACHINE_SCOPE
from douyin_online import desired, FIELD_NAMES


def row(quantity=1, parts=('12','34')):
    return dict(product_id='1234567890123456789', product_name='整机', sku_id='2234567890123456789',
                sku_name='配置', quantity=quantity, monitor_scope=MACHINE_SCOPE,
                erp=dict(source='maintenance', state='配件齐全', checked=123, unknown=[], shortages=[],
                         parts=[dict(goods_id=g, required=1, available=5) for g in parts]))


class LowStockTests(unittest.TestCase):
    def test_default_includes_zero_and_one_but_not_unknown_or_two(self):
        rules=validate({})
        for value in (0,1): self.assertTrue(is_low(row(value),rules))
        for value in (None,True,-1,2,'1'): self.assertFalse(is_low(row(value),rules))

    def test_exact_component_override_and_disable_precedence(self):
        self.assertEqual(threshold(row(),validate(dict(default=1,parts={'12':0,'34':3}))),3)
        self.assertEqual(threshold(row(parts=('12',)),validate(dict(default=1,parts={'12':0}))),0)
        self.assertFalse(is_low(row(0),validate(dict(parts={'12':9,'34':None}))))
        self.assertEqual(threshold(row(parts=('123',)),validate(dict(parts={'12':8}))),1)

    def test_invalid_rules_do_not_become_silent_disables(self):
        for value in (dict(default=True),dict(default=-1),dict(parts={'x':1}),dict(parts={'12':False})):
            with self.assertRaises(ValueError): validate(value)

    def test_rules_are_shop_scoped_rebuild_preview_and_preserve_snapshot_time(self):
        root=Path(__file__).resolve().parents[1]/'.codex_tmp/low-stock-tests'
        root.mkdir(parents=True,exist_ok=True)
        folder=Path(tempfile.mkdtemp(dir=root));store=DouyinInventory(folder)
        ids=[store.add_store(name) for name in ('店一','店二')]
        for ident in ids:
            store.rebase(ident,confirmed=True)
            r=row();r['export_sellable']=r['quantity']
            product=dict(product_id=r['product_id'],product_name=r['product_name'],monitor_scope=MACHINE_SCOPE,skus=[r])
            store.accept(ident,store.store(ident)['revision'],[product],complete=True,notify=False,now=1000)
        store.save_low_stock_rules(ids[0],dict(default=1,parts={'12':None}))
        store=DouyinInventory(folder)
        self.assertEqual(store.low_stock_rules(ids[0])['parts'],{'12':None})
        self.assertEqual(store.low_stock_rules(ids[1])['parts'],{})
        self.assertEqual(store.restock_preview(ids[0])['alerts'],[])
        self.assertEqual(len(store.restock_preview(ids[1])['alerts']),1)
        self.assertEqual(store.store(ids[0])['checked'],1000)
        fields={name:name for name in FIELD_NAMES}
        cells=next(iter(desired(store.restock_preview(ids[1]),fields).values()))
        self.assertEqual(cells['平台库存'],1)
        self.assertEqual(cells['补库存标记'],'低库存')
        self.assertEqual(store.restock_preview(ids[1])['sku_count'],0)

    def test_low_stock_with_erp_shortage_remains_an_alert_not_restockable(self):
        from douyin_restock import compose
        r=row();r['erp']['state']='缺货'
        preview=compose(dict(id='shop',name='店',field='export_sellable'),[r],inventory_at=1000)
        self.assertEqual(preview['sku_count'],0)
        self.assertEqual(len(preview['alerts']),1)
        cells=next(iter(desired(preview,{name:name for name in FIELD_NAMES}).values()))
        self.assertEqual(cells['补库存标记'],'低库存')
        self.assertEqual(cells['配件检查'],'缺货')
