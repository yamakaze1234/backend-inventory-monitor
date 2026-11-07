"""在线表同步验证：精确业务键、人工列保护、退出删行和未知结果不重放。"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
import json
import tempfile
import unittest

from douyin_inventory import DouyinInventory
from douyin_online import FIELD_NAMES, DwsTable, desired, reconcile, same, sync_online, status
from douyin_restock import brief
from douyin_scope import MACHINE_SCOPE


class FakeTable:
    """模拟云端持久化和数字/单选返回差异，不访问钉钉。"""
    def __init__(self):
        self.rows=[];self.writes=0;self.deletes=[];self.unknown_once=False

    def query(self):
        return deepcopy(self.rows)

    def write(self, operation, records):
        self.writes+=1
        for record in records:
            if operation == ['record','create']:
                self.rows.append(dict(recordId='record-'+str(len(self.rows)),cells=deepcopy(record['cells'])))
            else:
                next(r for r in self.rows if r['recordId']==record['recordId'])['cells'].update(deepcopy(record['cells']))
        if self.unknown_once:
            self.unknown_once=False;raise TimeoutError('已写入但响应超时')

    def delete(self, ids):
        self.deletes.extend(ids);self.rows=[r for r in self.rows if r['recordId'] not in ids]


class OnlineTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.codex_tmp/online-tests';base.mkdir(parents=True,exist_ok=True)
        self.store=DouyinInventory(Path(tempfile.mkdtemp(dir=base)))
        self.fields={name:'f'+str(i) for i,name in enumerate(FIELD_NAMES)}
        self.bind=dict(base_id='base',table_id='table',url='https://alidocs.dingtalk.com/i/nodes/example',profile='test-org:test-user',fields=self.fields)
        (self.store.root/'online-table.json').write_text(json.dumps(self.bind),encoding='utf-8')
        self.ident=self.store.add_store('测试店铺');self.store.rebase(self.ident,confirmed=True)
        self.client=FakeTable();self.accept()

    def accept(self, quantity=0, state='配件齐全'):
        product=dict(product_id='3797179547129085954',product_name='商品',monitor_scope=MACHINE_SCOPE,
                     skus=[dict(sku_id='3385942630294053',sku_name='配置一',export_sellable=quantity,
                     erp=dict(source='maintenance',state=state,checked=1800000000,unknown=[],shortages=[],
                              parts=[dict(required=1,available=2)]))])
        self.store.accept(self.ident,self.store.store(self.ident)['revision'],[product],complete=True,notify=False,now=1700000000)

    def sync(self, **kwargs):
        return sync_online(self.store,self.ident,client=self.client,**kwargs)

    def test_unmatched_in_stock_is_included_and_manual_note_preserved(self):
        self.accept(quantity=12, state='待核对')
        rows=self.store.snapshots(self.ident)
        rows[0]['erp']['matching_state']='sku_unmatched'
        config=self.store.store(self.ident)
        additions, changes, deletions, wanted=reconcile(config,None,rows,[],self.fields)
        self.assertEqual(len(additions),1)
        cells=additions[0]['cells']
        self.assertEqual(cells[self.fields['平台库存']],12)
        self.assertEqual(cells[self.fields['处理状态']],'sku未匹配 待人工核对')
        cells[self.fields['备注']]='人工备注'
        remote=[dict(recordId='existing',cells=cells)]
        additions,changes,deletions,_=reconcile(config,None,rows,remote,self.fields)
        self.assertEqual((additions,changes,deletions),([],[],[]))

    def test_brief_has_link_and_counts_but_no_sku_details(self):
        preview=self.store.restock_preview(self.ident);body=preview['payload']['markdown']['text']
        self.assertIn(self.bind['url'],body);self.assertIn('1 个链接 · 1 个零库存 SKU',body)
        self.assertNotIn('3385942630294053',body);self.assertLess(len(body),500)

    def test_number_string_equivalence_never_rounds_long_ids(self):
        self.assertTrue(same('0',0));self.assertTrue(same({'id':'opt','name':'待处理'},'待处理'))
        self.assertFalse(same('3797179547129085950','3797179547129085954'))

    def test_sync_creates_once_and_keeps_manual_status_and_notes(self):
        self.assertTrue(self.sync()['ok']);self.assertEqual(self.client.writes,1)
        cells=self.client.rows[0]['cells'];cells[self.fields['处理状态']]={'id':'done','name':'已补库存'}
        cells[self.fields['备注']]='等活动结束';cells[self.fields['平台库存']]='0'
        self.assertTrue(self.sync(force=True)['ok']);self.assertEqual(self.client.writes,1)
        self.assertEqual(cells[self.fields['备注']],'等活动结束')
        self.assertEqual(cells[self.fields['处理状态']]['name'],'已补库存')

    def test_exit_deletes_only_current_shops_managed_record(self):
        self.sync();other=deepcopy(self.client.rows[0]);other['recordId']='other-shop'
        other['cells'][self.fields['关联键']]='another-shop:3797179547129085954:3385942630294053'
        manual=dict(recordId='manual',cells={self.fields['店铺']:'测试店铺'})
        self.client.rows.extend([other,manual]);self.accept(quantity=3)
        result=self.sync(force=True)
        self.assertTrue(result['ok']);self.assertEqual(result['deleted'],1)
        self.assertEqual([r['recordId'] for r in self.client.rows],['other-shop','manual'])

    def test_unknown_committed_create_is_verified_without_replay(self):
        self.client.unknown_once=True;self.assertFalse(self.sync()['ok'])
        self.assertTrue(status(self.store.root,self.ident)['uncertain'])
        self.assertFalse(self.sync()['ok']);self.assertEqual(self.client.writes,1)
        result=self.sync(force=True)
        self.assertTrue(result['ok']);self.assertEqual(self.client.writes,1);self.assertEqual(len(self.client.rows),1)

    def test_duplicate_business_keys_stop_all_mutation(self):
        self.sync();self.client.rows.append(deepcopy(self.client.rows[0]));self.accept(quantity=3)
        result=self.sync(force=True)
        self.assertFalse(result['ok']);self.assertIn('重复',result['message']);self.assertEqual(self.client.deletes,[])

    def test_incomplete_query_is_rejected(self):
        client=DwsTable(self.bind);client.call=Mock(return_value=dict(complete=False,hasMore=True,records=[]))
        with self.assertRaisesRegex(ValueError,'未完整读取'):client.query()


if __name__=='__main__':
    unittest.main()
