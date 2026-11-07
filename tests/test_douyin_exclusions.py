"""人工排除的业务验证：跨店隔离、重启保留、精确删行和库存时间不变。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import queue
import tempfile
import unittest

from douyin_exclusions import parse_products
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_online import FIELD_NAMES, sync_online
from douyin_scope import MACHINE_SCOPE, product_url
from tests.test_douyin_online import FakeTable

PID='3797179547129085954'


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.codex_tmp/exclusion-tests';base.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=base));self.store=DouyinInventory(self.root)
        self.ident=self.shop('店铺一')
        self.fields={name:'f'+str(i) for i,name in enumerate(FIELD_NAMES)}
        binding=dict(base_id='fixture',table_id='fixture',url='https://example.com',profile='fixture',fields=self.fields)
        (self.store.root/'online-table.json').write_text(json.dumps(binding),encoding='utf-8')

    def shop(self, name):
        ident=self.store.add_store(name);self.store.rebase(ident,confirmed=True);self.accept(ident);return ident

    def accept(self, ident):
        rows=[dict(product_id=PID,product_name='样例主机',monitor_scope=MACHINE_SCOPE,skus=[
            dict(sku_id='123',sku_name='配置一',export_sellable=0,erp=dict(source='maintenance',state='配件齐全',checked=1800000000,
                 shortages=[],unknown=[],parts=[dict(required=1,available=4)]))])]
        self.store.accept(ident,self.store.store(ident)['revision'],rows,complete=True,now=1700000000)

    def test_parse_exact_ids_links_duplicates_and_invalid_batch(self):
        self.assertEqual(parse_products(PID+'\n'+product_url(PID)+'，3850449300903559731'),[PID,'3850449300903559731'])
        for value in ('', '3.797179547E18', PID+'\ninvalid', 'https://v.douyin.com/abc/',
                      'https://evil.example/?id='+PID,'https://haohuo.jinritemai.com/?id=12&product_id=34'):
            with self.subTest(value=value),self.assertRaises(ValueError): parse_products(value)

    def test_add_is_shop_scoped_and_survives_export_restart(self):
        other=self.shop('店铺二');before=self.store.snapshots(self.ident)
        self.store.update_exclusions(self.ident,PID)
        self.assertEqual(self.store.restock_preview(self.ident)['sku_count'],0)
        self.assertEqual(self.store.restock_preview(other)['sku_count'],1)
        self.assertEqual(self.store.snapshots(self.ident),before)
        self.accept(self.ident);self.store=DouyinInventory(self.root);self.store.migrate_restock()
        self.assertEqual(self.store.restock_preview(self.ident)['sku_count'],0)
        self.assertEqual(len(self.store.excluded_products(self.ident)),1)
        self.assertEqual(self.store.store(self.ident)['checked'],1700000000)

    def test_duplicate_is_idempotent_and_removal_recomputes_current(self):
        self.store.update_exclusions(self.ident,PID);revision=self.store.store(self.ident)['revision']
        self.assertEqual(self.store.update_exclusions(self.ident,PID)['changed'],0)
        self.assertEqual(self.store.store(self.ident)['revision'],revision)
        self.store.update_exclusions(self.ident,PID,remove=True)
        self.assertEqual(self.store.restock_preview(self.ident)['sku_count'],1)
        self.assertEqual(self.store.restock_preview(self.ident)['inventory_at'],1700000000)

    def test_invalid_batch_never_partially_saves(self):
        with self.assertRaises(ValueError): self.store.update_exclusions(self.ident,PID+'\ninvalid')
        self.assertEqual(self.store.excluded_products(self.ident),[])
        self.assertEqual(self.store.restock_preview(self.ident)['sku_count'],1)

    def test_stale_collect_cannot_overwrite_exclusion(self):
        revision=self.store.store(self.ident)['revision'];self.store.update_exclusions(self.ident,PID)
        with self.assertRaises(ValueError): self.store.accept(self.ident,revision,[],complete=True)
        self.assertEqual(self.store.restock_preview(self.ident)['sku_count'],0)

    def test_future_product_can_be_preexcluded_and_rebase_keeps_list(self):
        self.store.update_exclusions(self.ident,'999');self.store.rebase(self.ident,confirmed=True)
        self.assertEqual(self.store.excluded_products(self.ident)[0]['product_id'],'999')

    def test_online_deletes_only_managed_excluded_row(self):
        cloud=FakeTable();self.assertTrue(sync_online(self.store,self.ident,client=cloud)['ok'])
        own=cloud.rows[0]['recordId'];cloud.rows.append(dict(recordId='manual',cells={self.fields['备注']:'人工行'}))
        self.store.update_exclusions(self.ident,PID)
        result=sync_online(self.store,self.ident,client=cloud)
        self.assertTrue(result['ok']);self.assertEqual(cloud.deletes,[own]);self.assertEqual(cloud.rows[0]['recordId'],'manual')

    def test_runtime_serializes_save_and_requests_online_sync_without_sender(self):
        runtime=object.__new__(DouyinRuntime);runtime.store=self.store;runtime.commands=queue.Queue();runtime.sync_online=Mock()
        reply=runtime.submit_exclusions(self.ident,PID)
        self.assertEqual(self.store.excluded_products(self.ident),[])
        operation,ident=runtime.commands.get_nowait();runtime.execute(operation,ident)
        self.assertTrue(reply.get_nowait()['ok']);runtime.sync_online.assert_called_once_with(self.ident,force=True)
        self.assertFalse(runtime.notification_ready());sender=Mock();self.store.deliver(sender);sender.assert_not_called()
