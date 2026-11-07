"""重构验收：所有输入都是本地模拟数据，禁止真实通知与外部采集。"""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime, timezone

from inventory_sources import InventoryRuntimeService
from inventory_impact import InventoryImpact
from inventory_delivery import DeliveryWorker, format_events
from douyin_inventory import DouyinInventory
from tests.test_inventory_sources import Connector, SETTINGS, raw
from tests.test_profit_monitor import manifest, rehash


class RefactorTests(unittest.TestCase):
    def setUp(self):
        # 测试目录在验收后统一送回收站，不自动永久删除。
        root = Path(__file__).resolve().parents[1] / '.verification/refactor-20261105/test-data'
        root.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='case-', dir=root))
        self.s = InventoryRuntimeService(self.folder)
        self.c = Connector()
        self.c.rows.append(dict(raw(8), goods_id='102', 商品编码='SKU102', 商品名称='第二商品'))
        self.c.catalog = [dict(goods_id=str(i), 商品编码=f'SKU{i}', 商品名称=f'商品{i}', 总数量=8, 可销数=8, 待入=0) for i in (101,102)]
        self.now = time.time()
        self.s.configure_sql(SETTINGS, 'fixture-only', connector=self.c)
        self.s.sql_worker.step(self.now)
        for i in (101, 102):
            self.s.save_product(dict(sku=f'SKU{i}', goods_id=str(i), name=f'商品{i}', threshold=2))
        self.poll()

    def poll(self):
        self.now += 61
        self.s.request_check()
        self.s.sql_worker.step(self.now)

    def quantities(self):
        return {p['goods_id']:p['able'] for p in self.s.status()['products']}

    def config(self, **changes):
        value = manifest(goods_id='101')
        value['publishedAt'] = datetime.now(timezone.utc).isoformat()
        value.update(changes)
        rehash(value)
        path = self.folder / 'manifest.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def test_changed_code_does_not_block_good_product_and_does_not_rebind(self):
        self.c.rows[0].update(分库数=14, 分库可销数=14)
        self.c.rows[1]['商品编码'] = 'NEW102'
        self.c.catalog[1]['商品编码'] = 'NEW102'
        self.poll()
        self.assertEqual(self.quantities(), {'101':14,'102':None})
        self.assertEqual(self.s.status()['coverage']['counts'], dict(success=1,isolated=1,missing=0,warehouse_missing=0))
        self.assertEqual(self.s.sql_worker.failures, 0)
        self.assertEqual({e['sku'] for e in self.s.status()['events']}, {'SKU101'})
        self.assertEqual(self.s.products()[1]['sku'], 'SKU102')
        self.c.rows[1]['商品编码'] = self.c.catalog[1]['商品编码'] = 'SKU102'
        self.poll()
        self.assertEqual(self.quantities()['102'], 8)
        self.assertFalse(self.s.status()['data_issues'])

    def test_bad_numeric_is_isolated_but_missing_schema_rejects_round(self):
        self.c.rows[1]['分库数'] = 'bad'
        self.c.rows[0].update(分库数=14, 分库可销数=14)
        self.poll()
        self.assertEqual(self.quantities()['101'], 14)
        self.assertEqual(self.s.status()['coverage']['counts']['isolated'], 1)
        before = self.s.status()['coverage']
        del self.c.rows[1]['分库数']
        self.poll()
        self.assertEqual(self.s.status()['error_kind'], 'data')
        self.assertEqual(self.s.status()['coverage'], before)

    def test_cross_goods_code_collision_isolates_both(self):
        self.c.rows[1]['商品编码'] = self.c.catalog[1]['商品编码'] = 'SKU101'
        self.poll()
        self.assertEqual(self.s.status()['coverage']['counts']['isolated'], 2)
        self.assertFalse(self.s.status()['events'])

    def test_missing_product_and_missing_warehouse_are_distinct(self):
        self.c.rows = self.c.rows[:1]
        self.poll()
        self.assertEqual(self.s.status()['coverage']['counts']['warehouse_missing'], 1)
        self.c.catalog = self.c.catalog[:1]
        self.poll()
        self.assertEqual(self.s.status()['coverage']['counts']['missing'], 1)

    def test_pause_refresh_preserves_coverage_and_revision_marks_old(self):
        before = self.s.status()['coverage']
        self.s.set_enabled(False)
        self.s.sql_worker.refresh_catalog()
        self.now += 61
        self.s.sql_worker.step(self.now)
        self.assertEqual(self.s.status()['coverage'], before)
        product = self.s.products()[0]
        product['threshold'] += 1
        self.s.save_product(product)
        self.assertFalse(self.s.status()['coverage_current'])

    def test_excluded_watch_not_counted_as_success(self):
        with self.s.db() as db:
            self.s.put(db, 'sql_excluded_goods_ids', ['102'])
        self.poll()
        coverage = self.s.status()['coverage']
        self.assertEqual(coverage['expected'], 1)
        self.assertEqual(coverage['excluded'], 1)

    def test_manifest_matches_only_exact_goods_and_shows_missing_mapping(self):
        model = InventoryImpact(self.s)
        model.import_manifest(self.config())
        self.c.rows[0].update(分库数=0, 分库可销数=0)
        self.poll()
        row = model.report()['rows'][0]
        self.assertEqual(row['parts'][0]['goods_id'], '101')
        self.assertEqual(row['spu'], '')
        self.assertEqual(row['priority'], '待核对')
        # 即使名称完全相同，无精确编号的配件也不能自动匹配。
        path = self.config(revision=2)
        value = json.loads(path.read_text())
        value['configs'][0]['parts'][0]['goodsId'] = ''
        rehash(value)
        path.write_text(json.dumps(value), encoding='utf-8')
        model.import_manifest(path)
        self.assertFalse(model.report()['rows'][0]['parts'])

    def test_exact_listing_priority_requires_fresh_confirmed_store_and_config(self):
        model = InventoryImpact(self.s)
        model.import_manifest(self.config())
        self.c.rows[0].update(分库数=0, 分库可销数=0)
        self.poll()
        shops = DouyinInventory(self.folder)
        store = shops.add_store('测试店铺')
        shops.rebase(store, confirmed=True)
        product = [dict(product_id='9001', product_name='整机', skus=[dict(sku_id='9002', export_sellable=9)])]
        shops.accept(store, shops.store(store)['revision'], product, complete=True, notify=False)
        shops.enable(store, True)
        shops.accept(store, shops.store(store)['revision'], product, complete=True, notify=False)
        model.bind_listing('intel', 'c1', spu='26', store_id=store, product_id='9001', sku_id='9002')
        self.assertEqual(model.report()['rows'][0]['priority'], '优先核对')
        shops.fail(store)
        self.assertEqual(model.report()['rows'][0]['priority'], '待核对')
        shops.accept(store, shops.store(store)['revision'], product, complete=True, notify=False)
        self.assertEqual(model.report(now=time.time()+90000)['rows'][0]['priority'], '待核对')
        shops.rebase(store, confirmed=True)
        self.assertEqual(model.report()['rows'][0]['priority'], '待核对')

    def test_manifest_rollback_other_source_and_tamper_keep_last_import(self):
        model = InventoryImpact(self.s)
        model.import_manifest(self.config(revision=3))
        for changes in ({'revision':2}, {'revision':4,'workspaceId':'other'}):
            with self.assertRaises(ValueError):
                model.import_manifest(self.config(**changes))
            self.assertEqual(model.report()['revision'], 3)
        path = self.config(revision=4)
        value = json.loads(path.read_text())
        value['configs'][0]['name'] = 'tampered'
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(ValueError):
            model.import_manifest(path)

    def test_notifications_compact_keep_exact_names_and_explanation_once(self):
        self.c.rows[0].update(分库数=14, 分库可销数=14)
        self.c.rows[1].update(分库数=9, 分库可销数=8)
        self.poll()
        events = self.s.status()['events']
        body = format_events(events, self.s)
        self.assertEqual(body.count('采集时间：'), 1)
        self.assertEqual(body.count('不等同于确认入库'), 1)
        self.assertIn('8 → 14（+6）', body)
        self.assertIn('8 → 9（+1）', body)
        self.assertIn('可销 8 → 8（+0）', body)
        self.assertNotIn('预警值', body)

    def test_delivery_chunks_no_loss_and_unknown_chunk_never_retried(self):
        event = dict(sku='SKU101', name='商品101', kind='stock_increase', observed_at=time.time(),
                     cause='inventory_changed', threshold=2, before=dict(stock=8,able=8), after=dict(stock=9,able=8))
        with self.s.db() as db:
            for i in range(14):
                current = dict(event, id=f'e{i}')
                db.execute('INSERT INTO events(id,data,created) VALUES(?,?,?)', (current['id'],json.dumps(current),time.time()))
        calls = []
        worker = DeliveryWorker(self.s, lambda body: calls.append(body) or dict(status='unknown' if len(calls)==1 else 'sent'))
        self.assertEqual([worker.run_once() for _ in range(4)], [6,6,2,0])
        self.assertEqual(len(calls),3)
        with self.s.db() as db:
            self.assertEqual(dict(db.execute('SELECT status,count(*) FROM events GROUP BY status')), dict(unknown=6,sent=8))


if __name__ == '__main__':
    unittest.main()
