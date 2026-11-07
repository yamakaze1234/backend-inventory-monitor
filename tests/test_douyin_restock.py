"""验证提醒业务边界、跨店分组、配件到货重判与零投递保证。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest

from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_restock import eligible, compose
from douyin_scope import MACHINE_SCOPE


def item(pid='1234567890123456789', sid='2234567890123456789', quantity=0):
    """模拟维护表精确匹配和 ERP 实时核对后的有效 SKU。"""
    return dict(product_id=pid, product_name='测试整机', sku_id=sid, sku_name='配置一', quantity=quantity,
                monitor_scope=MACHINE_SCOPE, erp=dict(source='maintenance', state='配件齐全',
                parts=[dict(goods_id='12', required=2, available=5)], shortages=[], unknown=[], checked=1800000000))


class RestockTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.codex_tmp/restock-tests';base.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=base));self.store=DouyinInventory(self.root)
        self.ident=self.shop('测试店铺一')

    def shop(self, name):
        ident=self.store.add_store(name);self.store.rebase(ident,confirmed=True);return ident

    def accept(self, rows, ident=None, **kwargs):
        ident=ident or self.ident; products={}
        for row in rows:
            product=products.setdefault(row['product_id'], dict(product_id=row['product_id'],product_name=row['product_name'],
                monitor_scope=row['monitor_scope'],skus=[]))
            sku=deepcopy(row);sku['export_sellable']=row['quantity'];product['skus'].append(sku)
        self.store.accept(ident,self.store.store(ident)['revision'],list(products.values()),complete=True,now=1700000000,**kwargs)

    def test_only_verified_zero_and_complete_parts_qualify(self):
        self.assertTrue(eligible(item(),'export_sellable'))
        for value in (None,True,False,-1,2,'0',0.0):
            self.assertFalse(eligible(item(quantity=value),'export_sellable'))
        for override in (dict(state='缺货'),dict(state='待核对'),dict(source='erp'),dict(parts=[]),
                         dict(unknown=['库存未知']),dict(shortages=[{'goods_id':'12'}])):
            row=item();row['erp'].update(override);self.assertFalse(eligible(row,'export_sellable'))
        for available in (None,True,1,float('nan')):
            row=item();row['erp']['parts'][0]['available']=available
            self.assertFalse(eligible(row,'export_sellable'))
        self.assertFalse(eligible(item(),'dom_stock'))
        row=item();row['monitor_scope']='';self.assertFalse(eligible(row,'export_sellable'))

    def test_first_zero_creates_grouped_markdown_with_exact_ids(self):
        self.accept([item(),item(sid='2234567890123456790'),item(pid='1234567890123456790',sid='2234567890123456791'),item(sid='2234567890123456792',quantity=4)])
        preview=self.store.restock_preview(self.ident);body=preview['payload']['markdown']['text']
        self.assertEqual((preview['product_count'],preview['sku_count']),(2,3))
        self.assertIn('2 个链接 · 3 个零库存 SKU',body);self.assertNotIn('售罄',body)
        self.assertNotIn('2234567890123456789',body)
        self.assertEqual(len(preview['groups'][0]['skus']),2)
        self.assertIn('库存快照：2023-11-15 06:13:20',body)
        self.assertIn('配件核对：2027-01-15 16:00:00',body)
        self.assertEqual(len(self.store.restock_records(self.ident)),1)

    def test_shops_are_isolated_even_when_ids_are_same(self):
        other=self.shop('测试店铺二');self.accept([item()]);self.accept([item()],ident=other)
        first=self.store.restock_preview(self.ident);second=self.store.restock_preview(other)
        self.assertNotIn('测试店铺二',first['payload']['markdown']['text'])
        self.assertNotIn('测试店铺一',second['payload']['markdown']['text'])
        self.assertEqual(len(self.store.restock_records(other)),1)

    def test_unchanged_candidates_do_not_duplicate_after_restart(self):
        self.accept([item()]);self.store=DouyinInventory(self.root)
        row=item();row['erp']['parts'][0]['available']=99;row['erp']['checked']+=60
        self.accept([row]);self.assertEqual(len(self.store.restock_records(self.ident)),1)

    def test_zero_inventory_becomes_eligible_when_parts_arrive(self):
        row=item();row['erp'].update(state='缺货',shortages=['缺内存'])
        self.accept([row]);self.assertIsNone(self.store.restock_preview(self.ident)['payload'])
        self.accept([item()]);self.assertEqual(len(self.store.restock_records(self.ident)),1)

    def test_stock_recovery_or_unknown_clears_current_but_keeps_history(self):
        self.accept([item()]);self.accept([item(quantity=3)])
        self.assertIsNone(self.store.restock_preview(self.ident)['payload'])
        self.accept([item()]);self.assertEqual(len(self.store.restock_records(self.ident)),2)
        row=item();row['erp']['state']='待核对';self.accept([row])
        self.assertIsNone(self.store.restock_preview(self.ident)['payload'])
        self.assertEqual(len(self.store.restock_records(self.ident)),2)

    def test_missing_rows_clear_current_and_partial_round_preserves_current(self):
        self.accept([item()]);before=self.store.restock_preview(self.ident)
        with self.assertRaises(ValueError):
            self.store.accept(self.ident,self.store.store(self.ident)['revision'],[],complete=False)
        self.assertEqual(self.store.restock_preview(self.ident),before)
        self.accept([]);self.assertIsNone(self.store.restock_preview(self.ident)['payload'])

    def test_browser_change_invalidates_current_without_sending(self):
        self.accept([item()],notify=True)
        self.store.save_browser(self.ident,'edge')
        self.assertIsNone(self.store.restock_preview(self.ident))
        self.assertEqual(len(self.store.restock_records(self.ident)),1)
        sender=Mock();self.store.deliver(sender);sender.assert_not_called()

    def test_parts_recheck_updates_preview_without_refreshing_platform_time(self):
        row=item();row['erp']['state']='待核对';self.accept([row])
        runtime=object.__new__(DouyinRuntime);runtime.store=self.store;runtime.service=Mock()
        runtime.worker=Mock(side_effect=AssertionError('不应重新导出'))
        def attach(service,config,products):
            products[0]['skus'][0]['erp']=item()['erp']
        with patch('douyin_inventory_runtime.attach_maintenance',side_effect=attach):
            runtime.recheck_parts(self.ident)
        preview=self.store.restock_preview(self.ident)
        self.assertEqual(preview['inventory_at'],1700000000)
        self.assertEqual(preview['sku_count'],1)
        self.assertFalse(runtime.notification_ready())
        sender=Mock();self.store.deliver(sender);sender.assert_not_called()

    def test_product_names_cannot_inject_markdown_links_or_new_groups(self):
        row=item();row['product_name']='整机\n### 其他 [链接](https://bad.example)';row['sku_name']='配置*一*'
        data=compose(self.store.store(self.ident),[row],inventory_at=1700000000,detected_at=1800000000)
        body=data['payload']['markdown']['text']
        self.assertEqual(body.count('\n\n### '),0)
        self.assertNotIn('配置*一*',body)
        self.assertNotIn('[链接](',body)

    def test_upgrade_uses_saved_evidence_time_and_only_backfills_once(self):
        self.accept([item()])
        with self.store.db() as db:
            db.execute('DELETE FROM restock_previews');db.execute('DELETE FROM restock_records')
        self.store.migrate_restock();self.store.migrate_restock()
        preview=self.store.restock_preview(self.ident)
        self.assertEqual(preview['detected_at'],1800000000)
        self.assertEqual(preview['inventory_at'],1700000000)
        self.assertEqual(len(self.store.restock_records(self.ident)),1)


if __name__=='__main__':
    unittest.main()
