"""维护表身份、配件数量、跨来源失效及三天回收边界的回归验证。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from openpyxl import Workbook
from douyin_maintenance import read_maintenance, inspect_maintenance
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_erp import attach_maintenance
from douyin_export import ChromeExportWorker
from douyin_export_cleanup import cleanup_due, PERIOD
from douyin_scope import MACHINE_SCOPE

PID, SID = '1234567890123456789', '2234567890123456789'


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp/maintenance-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))

    def book(self, rows=None, *, shop=76):
        book = Workbook(); sheet = book.active; sheet.title = '综合表'
        sheet.append(['SPU','SKU','简称','CPU_id','CPU','CPU_数量','机箱_id','机箱','机箱_数量',
                      '显卡_id','显卡','显卡_数量','配件1_id','配件1','配件1_数量'])
        for row in rows or [[PID,SID,'配置一',12,'CPU',1,99,'机箱',1,'','无独立显卡',0,12,'重复CPU',2]]:
            sheet.append(row)
        hidden = book.create_sheet('禁止编辑'); hidden['G2'] = shop
        hidden['I6'] = ',{"goods_id":15,"n":2}'; hidden['G26'] = 99
        path = self.root / '维护表.xlsx'; book.save(path); book.close()
        return path

    def test_literal_empty_monitor_formula_does_not_hide_present_part(self):
        from openpyxl import load_workbook
        path=self.book()
        book=load_workbook(path);sheet=book['综合表']
        for col,name in enumerate(['显示器_id','显示器','显示器_数量','配件6_id','配件6','配件6_数量'],16):
            sheet.cell(1,col,name)
        sheet.cell(2,17,'=IF([@[配件6_id]]<>"",INDEX([主机成本表V2.xlsb]库存数据!$BD:$BD,MATCH([@[配件6_id]],[主机成本表V2.xlsb]库存数据!$BB:$BB,0)),"")').data_type='s'
        sheet.cell(2,18,1);book.save(path)
        self.assertEqual(read_maintenance(path,'综合表','76')['rows'][PID+':'+SID]['state'],'ready')
        sheet.cell(2,19,123);sheet.cell(2,21,1);book.save(path);book.close()
        self.assertIn('显示器',read_maintenance(path,'综合表','76')['rows'][PID+':'+SID]['state'])

    def test_explicit_integrated_gpu_is_not_a_missing_discrete_gpu(self):
        row=[PID,SID,'配置',12,'CPU',1,99,'机箱',1,'','全新Intel ARC Xe核显 支持8K 60HZ视频输出',0,'','',0]
        path=self.book([row])
        self.assertEqual(read_maintenance(path,'综合表','76')['rows'][PID+':'+SID]['state'],'ready')

    def test_exact_identity_extras_and_quantity(self):
        path = self.book(); info = inspect_maintenance(path)
        self.assertEqual(info['shop_id'], '76')
        row = read_maintenance(path, '综合表', '76')['rows'][PID+':'+SID]
        self.assertEqual(row['parts'], {'12':3,'99':1,'1651':1,'15':2})
        with self.assertRaisesRegex(ValueError, '店铺编号'):
            read_maintenance(path, '综合表', '32')

    def test_duplicates_missing_ids_and_invalid_quantity_are_not_guessed(self):
        row = [PID,SID,'配置一',12,'CPU',1,99,'机箱',1,'','无独立显卡',0,12,'CPU',2]
        path = self.book([row, row, [PID,'3234567890123456789',*row[2:5],0,*row[6:]],
                          [PID,int('4234567890123456789'),*row[2:]]])
        result = read_maintenance(path, '综合表', '76')['rows']
        self.assertIn('重复', result[PID+':'+SID]['state'])
        self.assertIn('正整数', result[PID+':3234567890123456789']['state'])
        self.assertNotIn(PID+':4234567890123456789', result)

    def test_cache_changes_when_saved_file_changes(self):
        path = self.book(); old = read_maintenance(path, '综合表', '76')
        self.book([[PID,SID,'配置二',12,'CPU',7,99,'机箱',1,'','无独立显卡',0,'','',0]])
        new = read_maintenance(path, '综合表', '76')
        self.assertNotEqual(old['sha256'], new['sha256'])
        self.assertEqual(new['rows'][PID+':'+SID]['parts']['12'], 7)

    def test_settings_are_per_shop_and_invalidate_old_result(self):
        store = DouyinInventory(self.root); a = store.add_store('英特尔官方旗舰店'); b = store.add_store('其他店')
        store.rebase(a, confirmed=True)
        product = dict(product_id=PID, monitor_scope=MACHINE_SCOPE, skus=[dict(sku_id=SID, export_sellable=8, erp={'state':'配件齐全'})])
        store.accept(a, store.store(a)['revision'], [product], complete=True, now=123)
        store.migrate_maintenance()
        self.assertEqual(store.snapshots(a)[0]['erp']['state'], '待核对')
        self.assertEqual(store.store(a)['maintenance_path'], '')
        store.save_maintenance(a, self.book(), '综合表', '76')
        self.assertEqual(store.store(b)['maintenance_path'], '')
        self.assertEqual(store.store(a)['checked'], 123)
        store.save_browser(a, 'edge', 'test-profile')
        self.assertFalse(store.store(a)['confirmed'])
        self.assertEqual(store.store(a)['browser'], 'edge')

    def test_runtime_recheck_keeps_platform_time_and_does_not_export(self):
        store = DouyinInventory(self.root); ident = store.add_store('英特尔官方旗舰店')
        store.rebase(ident, confirmed=True)
        product = dict(product_id=PID, monitor_scope=MACHINE_SCOPE, skus=[dict(sku_id=SID, export_sellable=8)])
        store.accept(ident, store.store(ident)['revision'], [product], complete=True, now=123)
        store.save_maintenance(ident, self.book(), '综合表', '76')
        runtime = object.__new__(DouyinRuntime); runtime.store = store
        runtime.service = SimpleNamespace(sql_worker=SimpleNamespace(source=None))
        runtime.worker = Mock(side_effect=AssertionError('不应重新导出'))
        runtime.recheck_parts(ident)
        self.assertEqual(store.store(ident)['checked'], 123)
        self.assertEqual(store.snapshots(ident)[0]['erp']['source'], 'maintenance')
        self.assertEqual(len(store.snapshots(ident)[0]['erp']['parts']), 4)
        self.assertEqual(store.events(), [])

    def test_attach_uses_only_sql_stock_not_erp_bom(self):
        from inventory_refactor_check import DemoSource
        from inventory_sources import InventoryRuntimeService
        service = InventoryRuntimeService(self.root); source = DemoSource()
        source.rows = source.rows[:4]; source.catalog = source.catalog[:4]
        for i, gid in enumerate(('12', '99', '1651', '15')):
            source.rows[i].update(goods_id=gid, 分库可销数=2)
            source.catalog[i].update(goods_id=gid)
        source.read_bundle = Mock(wraps=source.read_bundle)
        service.sql_worker.configure(source)
        with service.db() as db:
            service.put(db, 'scope', source.scope)
        config = dict(name='英特尔官方旗舰店', maintenance_path=str(self.book()), maintenance_sheet='综合表', maintenance_shop_id='76')
        products = [dict(product_id=PID, skus=[dict(sku_id=SID)])]
        with patch('douyin_erp.run_bridge', side_effect=AssertionError('不应查询 ERP 套餐')):
            attach_maintenance(service, config, products)
        self.assertEqual(products[0]['skus'][0]['erp']['source'], 'maintenance')
        self.assertEqual(products[0]['skus'][0]['erp']['state'], '缺货')
        self.assertEqual(products[0]['skus'][0]['erp']['shortages'][0]['shortage'], 1)
        self.assertEqual(source.read_bundle.call_count, 1)

    def test_browser_choice_and_profile_are_enforced(self):
        worker = object.__new__(ChromeExportWorker); worker.expected_shop='店铺'; worker.expected_browser='edge'; worker.browser_profile='edge-test'
        self.assertEqual(worker.browser_environment()['OPENCLI_PROFILE'], 'edge-test')
        worker.eval = Mock(return_value=dict(shop='店铺', url='https://fxg.jinritemai.com/ffa/g/list', browser='chrome'))
        with self.assertRaisesRegex(ValueError, '浏览器'):
            worker.check_shop()
        worker.eval.return_value['browser'] = 'edge'; worker.check_shop()

    def test_cleanup_three_days_keeps_latest_and_never_guesses_downloads(self):
        store=DouyinInventory(self.root); ident=store.add_store('店铺'); now=time.time()
        downloads=self.root/'Downloads'; downloads.mkdir()
        folder=store.root/'exports'/ident; folder.mkdir(parents=True)
        old=folder/'20260101_120000_abcdef.xlsx'; newest=folder/'20260102_120000_abcdef.xlsx'
        original=downloads/'productExport-owned.xlsx'; unrelated=downloads/'productExport-manual.xlsx'
        for path in (old,newest,original,unrelated):
            path.write_bytes(b'example'); os.utime(path,(now-PERIOD-10,now-PERIOD-10))
        old.with_suffix('.json').write_text(json.dumps(dict(file=str(old), download_file=str(original), sha256=hashlib.sha256(b'example').hexdigest())),encoding='utf-8')
        state=store.root/'export-cleanup.json'; state.write_text(json.dumps(dict(last_run=now-PERIOD+1)),encoding='utf-8')
        recycler=Mock()
        cleanup_due(store,now=now,recycler=recycler,downloads=downloads); recycler.assert_not_called()
        result=cleanup_due(store,now=now+2,recycler=recycler,downloads=downloads)
        self.assertEqual(result['recycled'],3)
        self.assertEqual([c.args[0] for c in recycler.call_args_list],[original,old,old.with_suffix('.json')])
        self.assertTrue(unrelated.exists()); self.assertTrue(newest.exists())

    def test_cleanup_failure_never_permanently_deletes(self):
        store=DouyinInventory(self.root); ident=store.add_store('店铺'); now=time.time()
        folder=store.root/'exports'/ident; folder.mkdir(parents=True)
        old=folder/'20260101_120000_abcdef.xlsx'; newest=folder/'20260102_120000_abcdef.xlsx'
        for path in (old,newest):
            path.write_bytes(b'example'); os.utime(path,(now-PERIOD-10,now-PERIOD-10))
        old.with_suffix('.json').write_text(json.dumps(dict(file=str(old),sha256=hashlib.sha256(b'example').hexdigest())),encoding='utf-8')
        (store.root/'export-cleanup.json').write_text(json.dumps(dict(last_run=now-PERIOD)),encoding='utf-8')
        result=cleanup_due(store,now=now,recycler=Mock(side_effect=OSError('失败')))
        self.assertTrue(result['errors']); self.assertTrue(old.exists()); self.assertTrue(old.with_suffix('.json').exists())


if __name__ == '__main__':
    unittest.main()
