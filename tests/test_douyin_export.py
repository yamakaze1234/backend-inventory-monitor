"""覆盖官方表格精度、完整性、预售口径及售罄/补货持久化边界。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import json
import os
import time
from datetime import datetime

from openpyxl import Workbook
from douyin_export import read_product_export, ChromeExportWorker, ChromeMachineExportWorker
from douyin_inventory import DouyinInventory


class ExportTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'douyin-export-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))

    def workbook(self, rows, *, categories=False):
        book = Workbook()
        sheet = book.active
        sheet.append(['商品ID', '商品名称', '规格ID（SKUID）', '商品规格', '现货可售', '预售库存', '阶梯库存'] +
                     (['一级类目', '二级类目', '三级类目', '四级类目'] if categories else []))
        for row in rows:
            sheet.append(row)
        path = self.root / 'official.xlsx'
        book.save(path)
        book.close()
        return path

    def test_presale_prevents_false_soldout_and_long_ids_remain_exact(self):
        path = self.workbook([['3852092376873041971', '商品', '3852092376873041999', '配置', '0', '7', '3']])
        products = read_product_export(path, 1)
        self.assertEqual(products[0]['skus'][0]['export_sellable'], 10)
        self.assertEqual(products[0]['skus'][0]['sku_id'], '3852092376873041999')

    def test_bad_worksheet_dimension_does_not_truncate_rows(self):
        path = self.workbook([['123', '商品', '456', '配置', '1', '0', '0']])
        with zipfile.ZipFile(path) as z:
            members = {n: z.read(n) for n in z.namelist()}
        members['xl/worksheets/sheet1.xml'] = members['xl/worksheets/sheet1.xml'].replace(b'ref="A1:G2"', b'ref="A1"')
        with zipfile.ZipFile(path, 'w') as z:
            for name, data in members.items():
                z.writestr(name, data)
        self.assertEqual(read_product_export(path, 1)[0]['skus'][0]['export_sellable'], 1)

    def test_partial_export_duplicate_and_numeric_id_are_rejected(self):
        row = ['123', '商品', '456', '配置', '0', '0', '0']
        for rows, expected in [([row], 2), ([row, row], 1), ([[123, *row[1:]]], 1)]:
            with self.subTest(rows=rows, expected=expected), self.assertRaises(ValueError):
                read_product_export(self.workbook(rows), expected)

    def test_unknown_component_never_becomes_zero(self):
        for value in [None, '-', -2, True]:
            path = self.workbook([['123', '商品', '456', '配置', '0', value, '0']])
            self.assertIsNone(read_product_export(path, 1)[0]['skus'][0]['export_sellable'])

    def test_machine_categories_are_required_and_consistent(self):
        """没有类目不能猜整机；同商品类目冲突也必须保留旧快照。"""
        row = ['123', '名字含整机也不能猜', '456', '配置', '0', '0', '2']
        with self.assertRaisesRegex(ValueError, '类目'):
            read_product_export(self.workbook([row]), 1, require_category=True)
        machine = ['3C数码及配件', '电脑', 'DIY电脑', 'DIY兼容机']
        part = ['3C数码及配件', '电脑组件', '显卡', '']
        from douyin_scope import MACHINE_SCOPE
        rows = read_product_export(self.workbook([row + machine, ['789', *row[1:]] + part], categories=True),
                                   2, require_category=True)
        self.assertEqual(rows[0]['monitor_scope'], MACHINE_SCOPE)
        self.assertEqual(rows[1]['monitor_scope'], '')
        self.assertEqual(rows[0]['skus'][0]['export_sellable'], 2)
        self.assertIn('上下架未核对', rows[0]['skus'][0]['stock_state'])
        with self.assertRaisesRegex(ValueError, '类目不一致'):
            read_product_export(self.workbook([row + machine, ['123', '商品', '457', *row[3:]] + part], categories=True),
                                1, require_category=True)

    def test_machine_runtime_keeps_erp_and_rebases_when_source_changes(self):
        """导出接替页面时不伪造库存归零；本地筛选后仍调用 ERP，绝不查询单品。"""
        import threading
        from douyin_inventory_runtime import DouyinRuntime
        from douyin_scope import MACHINE_SCOPE
        store = DouyinInventory(self.root)
        ident = store.add_store('店铺'); store.rebase(ident, confirmed=True)
        revision = store.store(ident)['revision']
        old = dict(product_id='123', monitor_scope=MACHINE_SCOPE, skus=[dict(sku_id='456', dom_stock=8)])
        store.accept(ident, revision, [old], complete=True, field='dom_stock')
        with store.db() as db:
            db.execute('UPDATE stores SET enabled=1 WHERE id=?', (ident,))
        runtime = object.__new__(DouyinRuntime)
        runtime.store, runtime.service, runtime.stop_event = store, Mock(), threading.Event()
        worker = Mock()
        product = dict(product_id='123', monitor_scope=MACHINE_SCOPE, skus=[dict(sku_id='456', export_sellable=0)])
        worker.call.return_value = dict(products=[product], complete=True, field='export_sellable')
        runtime.worker = Mock(return_value=worker)
        with patch('douyin_inventory_runtime.attach_maintenance') as erp:
            runtime.collect(ident)
            self.assertEqual(erp.call_args.args[1]['id'], ident)
            self.assertEqual(erp.call_args.args[2], [product])
        self.assertEqual(store.store(ident)['field'], 'export_sellable')
        self.assertEqual(store.events(), [])
        before = store.snapshots(ident)
        product['monitor_scope'] = ''
        with patch('douyin_inventory_runtime.attach_maintenance') as erp, self.assertRaisesRegex(ValueError, '整机范围'):
            runtime.collect(ident)
        erp.assert_not_called()
        self.assertEqual(store.snapshots(ident), before)

    def test_restock_and_zero_are_once_per_transition_across_restart(self):
        store = DouyinInventory(self.root)
        ident = store.add_store('店铺')
        store.rebase(ident, confirmed=True)
        def accept(n):
            return store.accept(ident, store.store(ident)['revision'],
                [dict(product_id='123', skus=[dict(sku_id='456', export_sellable=n)])], complete=True)
        accept(0)
        store.enable(ident, True)
        self.assertEqual(accept(0), [])
        self.assertEqual(len(accept(8)), 1)
        self.assertEqual(store.events()[0]['kind'], 'restocked')
        store = DouyinInventory(self.root)
        self.assertEqual(accept(8), [])
        self.assertEqual(len(accept(0)), 1)
        self.assertEqual(accept(0), [])
        sender = Mock(return_value={'status': 'sent'})
        store.deliver(sender)
        store.deliver(sender)
        # 库存变化继续留档，但不再作为补库存提醒发送。
        sender.assert_not_called()
        self.assertTrue(all(event['status'] == 'preview' for event in store.events()))

    def test_failed_round_preserves_valid_baseline(self):
        store = DouyinInventory(self.root)
        ident = store.add_store('店铺')
        store.rebase(ident, confirmed=True)
        revision = store.store(ident)['revision']
        rows = [dict(product_id='123', skus=[dict(sku_id='456', export_sellable=8)])]
        store.accept(ident, revision, rows, complete=True)
        before = store.snapshots(ident)
        store.fail(ident, '下载失败')
        with self.assertRaises(ValueError):
            store.accept(ident, revision, [], complete=False)
        self.assertEqual(store.snapshots(ident), before)

    def test_legacy_basis_is_paused_and_pending_events_cancelled(self):
        store = DouyinInventory(self.root)
        ident = store.add_store('旧店铺', 'total_stock')
        store.rebase(ident, confirmed=True)
        store.migrate_export()
        self.assertEqual(store.store(ident)['field'], 'export_sellable')
        self.assertFalse(store.store(ident)['confirmed'])
        self.assertFalse(store.store(ident)['enabled'])

    def test_wrong_shop_is_rejected_before_export(self):
        worker = object.__new__(ChromeExportWorker)
        worker.expected_shop = '英特尔官方旗舰店'
        worker.eval = Mock(return_value={'shop': '另一店铺', 'url': 'https://fxg.jinritemai.com/ffa/g/list'})
        with self.assertRaisesRegex(ValueError, '店铺'):
            worker.check_shop()

    def test_existing_inventory_robot_cannot_receive_douyin_messages(self):
        """现有配置讨论群不自动成为抖店通知目标。"""
        from douyin_inventory_runtime import DouyinRuntime
        runtime = object.__new__(DouyinRuntime)
        runtime.live, runtime.service = True, Mock()
        robots = Mock()
        with patch('notification_robots.store_for_service', return_value=robots):
            robots.list_robots.return_value = [dict(enabled=True, channels=['inventory', 'daily', 'messages'])]
            self.assertFalse(runtime.notification_ready())
            robots.list_robots.return_value = [dict(enabled=False, channels=['douyin_inventory'])]
            self.assertFalse(runtime.notification_ready())
            robots.list_robots.return_value = [dict(enabled=True, channels=['douyin_inventory'])]
            # 用户已授权恢复发送，只有显式订阅抖店频道的启用机器人可以接收。
            self.assertTrue(runtime.notification_ready())

    def test_pending_export_resumes_without_submitting_and_rejects_stale_download(self):
        """平台慢任务跨轮续查；旧下载不能冒充本轮新文件。"""
        path = self.workbook([['123', '商品', '456', '配置', '1', '0', '0']])
        worker = object.__new__(ChromeExportWorker)
        worker.root, worker.ident, worker.page, worker.expected_shop = self.root, 'case', 'tab-id', '店铺'
        worker.open_page = worker.check_shop = worker.click = worker.pause = Mock()
        worker.request_export = Mock(side_effect=AssertionError('不应重建报表'))
        started = time.time() - 1
        worker.records = Mock(return_value=[dict(key='1', time=datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M:%S'), ready=True)])
        worker.cli = Mock(return_value=dict(downloaded=True, state='complete', filename=str(path)))
        pending = self.root / 'chrome/case.pending.json'
        pending.parent.mkdir()
        data = dict(shop='店铺', started=started, count=1, old=[])
        pending.write_text(json.dumps(data), encoding='utf-8')
        os.utime(path, (started-600, started-600))
        with self.assertRaisesRegex(ValueError, '完整下载'):
            worker.call('collect')
        self.assertEqual(json.loads(pending.read_text(encoding='utf-8')), data)
        os.utime(path, None)
        result = worker.call('collect')
        self.assertEqual(result['export']['sku_count'], 1)
        self.assertEqual(json.loads(pending.read_text(encoding='utf-8')), {})
        worker.request_export.assert_not_called()

    def test_machine_worker_validates_all_products_before_filtering(self):
        """全表含单品也要核验总数，但主快照和 ERP 输入只包含整机。"""
        row = ['123', '整机', '456', '配置', '0', '0', '3']
        path = self.workbook([row + ['3C数码及配件', '电脑', 'DIY电脑', 'DIY兼容机'],
                              ['789', '显卡', '999', '单品', '1', '0', '0',
                               '3C数码及配件', '电脑组件', '显卡', '']], categories=True)
        worker = object.__new__(ChromeMachineExportWorker)
        worker.root, worker.ident, worker.page, worker.expected_shop = self.root, 'case', 'tab', '店铺'
        worker.open_page = worker.check_shop = worker.click = worker.pause = Mock()
        started = time.time() - 1
        worker.records = Mock(return_value=[dict(key='1', time=datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M:%S'), ready=True)])
        worker.cli = Mock(return_value=dict(downloaded=True, state='complete', filename=str(path)))
        pending = self.root / 'chrome/case.pending.json'; pending.parent.mkdir()
        pending.write_text(json.dumps(dict(shop='店铺', started=started, count=2, old=[])), encoding='utf-8')
        result = worker.call('collect')
        self.assertEqual([p['product_id'] for p in result['products']], ['123'])
        self.assertEqual(result['export']['product_count'], 2)
        self.assertEqual(result['export']['monitor_product_count'], 1)
        self.assertEqual(result['field'], 'export_sellable')
        self.assertEqual(result['products'][0]['skus'][0]['export_sellable'], 3)

    def test_export_zero_needs_sale_status_before_restock_advice(self):
        """表格库存为零只能建议核对售卖状态，不能自动断言在售缺货。"""
        from douyin_erp import enrich_products
        products = [dict(product_id='123', skus=[dict(sku_id='456', source='export', export_sellable=0)])]
        mapped = dict(rows={'123:456':dict(state='ready', parts={'12':1}, goods_id='900', observed=123)})
        enrich_products(products, mapped, {'12':{'able':8}})
        self.assertEqual(products[0]['skus'][0]['erp']['action'], '核对售卖状态后补库存')


if __name__ == '__main__':
    unittest.main()
