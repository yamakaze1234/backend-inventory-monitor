"""页面巡检边界：不把预售、下架、占用、采集失败或 ERP 缺失误认为售罄/齐货。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
import threading

from douyin_stock_page import parse_product
from douyin_erp import assess
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_erp import attach_erp, enrich_products
from inventory_monitor import InventoryService
from douyin_scope import MACHINE_SCOPE, MACHINE_CATEGORIES, machine_rows, product_url


def page(total='3\n现货 0\n预售批次 3', available='3', occupied='-', sale='售卖中', sku_status='已上架'):
    return {'category': MACHINE_CATEGORIES[0], 'rows': [
        {'key': 'product-123', 'expanded': True,
         'cells': ['商品\n商品ID：123\n现货预售混合', '普通库存', '2026-11-06\n'+sale, total, available, occupied, '-', '编辑库存']},
        {'key': 'sku-3390250654999845', 'expanded': False,
         'cells': ['配置一\nSKU ID: 3390250654999845', '普通库存', sku_status, total, available, occupied, '-', '编辑库存']}]}


class PageTests(unittest.TestCase):
    def test_machine_scope_not_guessed_from_name(self):
        product = parse_product(page(), '123')
        self.assertEqual(product['monitor_scope'], MACHINE_SCOPE)
        self.assertIn('id=123', product['product_url'])
        single = page()
        single['category'] = '电脑配件/CPU'
        self.assertEqual(machine_rows([parse_product(single, '123'), product]), [product])
        self.assertEqual(product_url('javascript:alert(1)'), '')

    def test_scope_switch_starts_new_baseline(self):
        # 相同 SKU 从旧全品类切到整机，不应虚构一次售罄事件。
        base = Path(__file__).resolve().parents[1] / '.codex_tmp/douyin-page-tests'
        base.mkdir(parents=True, exist_ok=True)
        store = DouyinInventory(Path(tempfile.mkdtemp(dir=base)))
        ident = store.add_store('店铺'); store.rebase(ident, confirmed=True)
        revision = store.store(ident)['revision']
        old = parse_product(page('3', '3'), '123'); old.pop('monitor_scope')
        store.accept(ident, revision, [old], complete=True, field='dom_stock')
        with store.db() as db:
            db.execute('UPDATE stores SET enabled=1 WHERE id=?', (ident,))
        self.assertEqual(store.accept(ident, revision, [parse_product(page('0','0'),'123')], complete=True,
                                     field='dom_stock', notify=False), [])
        self.assertEqual(len(store.accept(ident, revision, [parse_product(page('2','2'),'123')], complete=True,
                                         field='dom_stock', notify=False)), 1)

    def test_presale_and_full_sku_id(self):
        sku = parse_product(page(), '123')['skus'][0]
        self.assertEqual(sku['dom_stock'], 3)
        self.assertEqual(sku['stock_state'], '预售有库存')
        self.assertEqual(sku['sku_id'], '3390250654999845')

    def test_off_shelf_is_never_zero(self):
        for sale, status in [('已下架','已上架'), ('售卖中','已下架')]:
            sku = parse_product(page('0','0',sale=sale,sku_status=status), '123')['skus'][0]
            self.assertIsNone(sku['dom_stock'])
            self.assertEqual(sku['stock_state'], '已下架')

    def test_occupied_and_inconsistent_are_unknown(self):
        for total, available, occupied in [('3','0','3'), ('3','3','2'), ('-','0','-')]:
            self.assertIsNone(parse_product(page(total,available,occupied),'123')['skus'][0]['dom_stock'])

    def test_real_zero_and_positive_stock(self):
        self.assertEqual(parse_product(page('0','0'),'123')['skus'][0]['stock_state'],'在售库存为零')
        self.assertEqual(parse_product(page('8','8'),'123')['skus'][0]['dom_stock'],8)

    def test_collapsed_duplicate_missing_and_incomplete_rejected(self):
        cases = [page() for _ in range(5)]
        cases[0]['rows'][0]['expanded'] = False
        cases[1]['rows'].append(dict(cases[1]['rows'][1]))
        cases[2]['rows'].pop()
        cases[3]['rows'][1]['cells'][3] = '2'
        cases[4]['rows'][1]['cells'][0] = '配置\nSKU ID: 99'
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_product(data,'123')

    def test_source_switch_and_offshelf_break_transition(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp/douyin-page-tests'
        base.mkdir(parents=True,exist_ok=True)
        store = DouyinInventory(Path(tempfile.mkdtemp(dir=base)))
        ident = store.add_store('店铺'); store.rebase(ident,confirmed=True)
        revision = store.store(ident)['revision']
        store.accept(ident,revision,[dict(product_id='123',skus=[dict(sku_id='3390250654999845',export_sellable=8)])],complete=True)
        with store.db() as db:
            db.execute('UPDATE stores SET enabled=1 WHERE id=?',(ident,))
        def accept(snapshot):
            return store.accept(ident,revision,[parse_product(snapshot,'123')],complete=True,notify=False,field='dom_stock')
        self.assertEqual(accept(page('0','0')),[])
        self.assertEqual(len(accept(page('3','3'))),1)
        self.assertEqual(accept(page('0','0',sale='已下架')),[])
        self.assertEqual(accept(page('0','0')),[])
        self.assertEqual(store.events()[0]['status'],'preview')
        store.migrate_export()
        self.assertEqual(store.store(ident)['field'],'dom_stock')

    def test_preview_notifications_disabled(self):
        runtime=object.__new__(DouyinRuntime)
        runtime.live=False;runtime.service=Mock()
        self.assertFalse(runtime.notification_ready())

    def test_runtime_persists_dom_and_erp_unknown_without_sql(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp/douyin-page-tests'
        base.mkdir(parents=True, exist_ok=True)
        service=InventoryService(Path(tempfile.mkdtemp(dir=base)))
        runtime=object.__new__(DouyinRuntime)
        runtime.service=service;runtime.store=DouyinInventory(service.root)
        runtime.stop_event=threading.Event();runtime.live=True
        ident=runtime.store.add_store('店铺');runtime.store.rebase(ident,confirmed=True)
        worker=Mock();worker.call.return_value=dict(products=[parse_product(page(),'123')],complete=True,field='dom_stock')
        runtime.worker=Mock(return_value=worker)
        runtime.collect(ident)
        row=runtime.store.snapshots(ident)[0]
        self.assertEqual(runtime.store.store(ident)['field'],'dom_stock')
        self.assertEqual(row['stock_state'],'预售有库存')
        self.assertEqual(row['erp']['state'],'待核对')
        self.assertEqual(runtime.store.events(),[])


class ErpTests(unittest.TestCase):
    def test_exact_mapping_and_suggestions(self):
        products=[parse_product(page('0','0'),'123')]
        mapped={'rows':{'123:3390250654999845':dict(state='ready',parts={'12':2},goods_id='900',observed=123)}}
        enrich_products(products,mapped,{'12':{'able':8}})
        self.assertEqual(products[0]['skus'][0]['erp']['action'],'可补平台库存')
        enrich_products(products,{'rows':{}},{'12':{'able':8}})
        self.assertEqual(products[0]['skus'][0]['erp']['state'],'待核对')

    def test_per_unit_requirement_and_incoming_not_usable(self):
        result=assess({'12':2,'13':1},{'12':{'name':'内存','able':1,'purchase':100},'13':{'able':10}})
        self.assertEqual(result['state'],'缺货')
        self.assertEqual(result['buildable'],0)
        self.assertEqual(result['shortages'][0]['shortage'],1)
        self.assertEqual(result['shortages'][0]['incoming'],100)

    def test_missing_stock_does_not_mean_available(self):
        result=assess({'12':1},{})
        self.assertEqual(result['state'],'待核对')
        self.assertIsNone(result['buildable'])

    def test_known_shortage_survives_another_unknown_part(self):
        result=assess({'12':1,'13':1},{'12':{'able':-4}})
        self.assertEqual(result['state'],'缺货')
        self.assertIsNone(result['buildable'])
        self.assertEqual(result['shortages'][0]['shortage'],1)

    def test_buildable_is_minimum_not_sum(self):
        self.assertEqual(assess({'12':2,'13':1},{'12':{'able':9},'13':{'able':7}})['buildable'],4)


if __name__=='__main__':
    unittest.main()
