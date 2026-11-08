"""机器人数据契约回归：完整分页、精确身份、只读授权和敏感字段隔离。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from unittest.mock import patch
from types import SimpleNamespace
import unittest
import urllib.request
from urllib.error import HTTPError

from inventory_refactor_check import seed
from inventory_business import BusinessConsole
from inventory_console import InventoryConsole
from inventory_data_api import catalog, query, DataError
from douyin_inventory import DouyinInventory
from order_bot_adapter import OrderBotAdapter


class DataApiTests(unittest.TestCase):
    def setUp(self):
        output=Path(__file__).resolve().parents[1]/'outputs/query-api-tests';output.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=output));self.service=seed(self.root)
        self.runtime=SimpleNamespace(store=DouyinInventory(self.root))
        self.business=BusinessConsole(self.service,SimpleNamespace(root=self.root),self.runtime,offline=True)
        self.assets=self.root/'assets';self.assets.mkdir();(self.assets/'index.html').write_text('test')
        self.bot=OrderBotAdapter(state_root=self.root/'orders')
        self.console=InventoryConsole(self.service,bot=self.bot,business=self.business,assets=self.assets)
        with self.service.db() as db:
            self.service.put(db,'daily_source_root',str(self.root))
            self.service.put(db,'sql_connection',dict(password='SECRET-SQL'))
            self.service.put(db,'ai',dict(key='SECRET-AI'))

    def tearDown(self):
        self.console.close();self.service.close_source()

    def ask(self,**values):return query(self.console,values)

    def request(self,path,body=None,token=None):
        headers={'X-Console-Token':token or self.console.query_token}
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(self.console.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with urllib.request.urlopen(req) as response:return json.load(response)

    def seed_orders(self):
        self.bot.state_root.mkdir()
        with closing(sqlite3.connect(self.bot.state_root/'jobs.sqlite3')) as db:
            db.execute('CREATE TABLE jobs(id TEXT,kind TEXT,number TEXT,shop TEXT,state TEXT,created REAL,delivery TEXT)')
            db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',[(str(i),'order',str(90000000000000000+i),'76' if i%2 else '32','verified',i,'sent') for i in range(310)])
            db.commit()

    def test_catalog_all_sources_readable_and_no_secret_or_side_effect(self):
        before={p:p.read_bytes() for p in self.root.rglob('*.sqlite3')}
        for dataset in catalog()['datasets']:
            result=self.ask(dataset=dataset['id'])
            text=json.dumps(result)
            self.assertNotIn('SECRET',text);self.assertNotIn('_forms',text)
            self.assertTrue(result['read_only']);self.assertFalse(result['live_query'])
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*.sqlite3')})
        self.assertFalse((self.root/'profit_monitor.sqlite3').exists())
        self.assertFalse(self.bot.state_root.exists())

    def test_orders_complete_pagination_and_exact_shop(self):
        self.seed_orders()
        request=dict(dataset='orders',filters=[dict(field='shop',op='eq',value='76')],page_size=40,sort=dict(field='created',direction='desc'))
        first=self.ask(**request);self.assertEqual(155,first['total'])
        all_rows=[]
        for page in range(4):
            result=self.ask(**request,page=page,snapshot=first['snapshot']);all_rows.extend(result['rows'])
        self.assertEqual(155,len(all_rows));self.assertEqual(155,len({r['id'] for r in all_rows}))
        self.assertTrue(all(r['shop']=='76' for r in all_rows));self.assertIsInstance(all_rows[0]['number'],str)

    def test_snapshot_change_refuses_mixed_pages(self):
        self.seed_orders();first=self.ask(dataset='orders')
        with closing(sqlite3.connect(self.bot.state_root/'jobs.sqlite3')) as db:
            db.execute("UPDATE jobs SET state='failed' WHERE id='1'");db.commit()
        with self.assertRaises(DataError) as error:self.ask(dataset='orders',page=1,snapshot=first['snapshot'])
        self.assertEqual('SNAPSHOT_CHANGED',error.exception.code)

    def test_time_range_beijing_end_date_and_field_validation(self):
        self.seed_orders()
        result=self.ask(dataset='orders',start='1970-01-01',end='1970-01-01')
        self.assertEqual(310,result['total'])
        for request in [dict(dataset='orders',filters=[dict(field='secret',op='eq',value='x')]),dict(dataset='orders',sql='DROP TABLE jobs'),dict(dataset='orders',page_size=201),dict(dataset='products',start='2026-01-01')]:
            with self.assertRaises(DataError):self.ask(**request)

    def test_query_token_cannot_use_write_business_or_get_full_token(self):
        self.assertEqual('inventory-data/v1',self.request('/api/data/catalog')['version'])
        self.assertTrue(self.request('/api/data/query',dict(dataset='products'))['read_only'])
        for path,body in [('/api/orders/action',dict(action='start')),('/api/business/action',{}),('/api/update/install',{}),('/api/data/connection',None),('/api/inventory',None)]:
            with self.assertRaises(HTTPError) as error:self.request(path,body)
            self.assertEqual(403,error.exception.code)
        with self.assertRaises(HTTPError) as error:self.request('/api/data/query',dict(dataset='not-real'))
        self.assertEqual('UNKNOWN_DATASET',json.load(error.exception)['code'])

    def test_price_configs_and_history_keep_identity_and_costs(self):
        monitor=self.business.profit()
        with monitor.db() as db:
            db.execute("INSERT INTO price_results(id,run_id,source_id,workspace_id,goods_id,valid,unit_cents,previous_cents,changed,reason,details,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ('r1','run','source','workspace','123',1,12000,10000,1,'',json.dumps(dict(names=['配件'],refs=[['76','same','链接甲','配置甲','内存'],['32','same','链接乙','配置乙','内存']])),1))
        row=self.ask(dataset='price_results')['rows'][0]
        self.assertEqual((10000,12000),(row['previous_cents'],row['unit_cents']))
        self.assertEqual(['76','32'],[r['shop_id'] for r in row['configs']])
        self.assertNotIn('sql_scope',row)

    def test_combined_supply_view_retains_daily_action_routing(self):
        self.business.controller.status=lambda:dict(state='paused',ready=0,sources=[])
        result=self.business.snapshot('dingtalk',{})
        sections={r['id']:r for r in result['sections']}
        self.assertIn('weekly',sections)
        self.assertEqual('daily',sections['weekly']['forms'][0]['module'])



    def test_followup_actions_inherit_actual_job_module(self):
        from inventory_business_actions import ACTIONS
        with patch.dict(ACTIONS,{('daily','generate'):lambda b,v:dict(forms=[dict(action='report-save'),dict(action='report-send'),dict(action='robot-save',module='settings')])}):
            self.business.submit('daily','generate',{},'daily-followup-01')
            deadline=time.monotonic()+2
            while self.business.busy() and time.monotonic()<deadline:time.sleep(.01)
        forms=self.business.job('daily-followup-01')['result']['forms']
        self.assertEqual(['daily','daily','settings'],[f['module'] for f in forms])


    def test_stock_query_preserves_shortage_incoming_and_warehouse(self):
        from douyin_scope import MACHINE_SCOPE
        store=self.runtime.store;shop=store.add_store('隔离店铺')
        record=dict(store_id=shop,product_id='123',product_name='整机',sku_id='456',sku_name='配置',quantity=0,monitor_scope=MACHINE_SCOPE,
            erp=dict(state='缺货',warehouse='公司大库',checked=100,buildable=0,parts=[dict(goods_id='789',name='内存',required=2,available=1)],
                     shortages=[dict(goods_id='789',name='内存',required=2,available=1,shortage=1,incoming=5)],unknown=[]))
        with store.db() as db:db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?)',(shop,'123','456',json.dumps(record),0,100))
        row=self.ask(dataset='douyin_stock',filters=[dict(field='store_id',op='eq',value=shop)])['rows'][0]
        self.assertEqual(shop,row['store_id']);self.assertEqual('公司大库',row['erp_warehouse'])
        self.assertEqual(1,row['erp_shortages'][0]['shortage']);self.assertEqual(5,row['erp_shortages'][0]['incoming'])
        self.assertEqual(1,row['erp_parts'][0]['available'])


    def test_price_batches_are_not_limited_to_ui_dropdown(self):
        monitor=self.business.profit()
        with monitor.db() as db:
            db.executemany("INSERT INTO runs(id,started,status,revision,kind) VALUES(?,?,'completed',1,'price-v2')",[(str(i),i) for i in range(135)])
        first=self.ask(dataset='price_batches',page_size=100)
        self.assertEqual(135,first['total']);self.assertTrue(first['has_more'])
        last=self.ask(dataset='price_batches',page_size=100,page=1,snapshot=first['snapshot'])
        self.assertEqual(35,len(last['rows']));self.assertFalse(last['has_more'])
        earliest=self.ask(dataset='price_batches',filters=[dict(field='id',op='eq',value='0')])
        self.assertEqual(1,earliest['total'])

    def test_read_only_refresh_is_scoped_and_jobs_are_isolated(self):
        from inventory_business_actions import ACTIONS
        with patch.dict(ACTIONS,{('profit','view-changes'):lambda b,v:dict(message='模拟成本查询完成')}):
            result=self.request('/api/data/refresh',dict(dataset='price_changes',request_id='query-price-001'))
            deadline=time.monotonic()+2
            while self.business.busy() and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual('done',self.request('/api/data/job?id='+result['job'])['state'])
        self.business.jobs['other-business-job']=dict(module='settings',action='robot-save',state='done')
        with self.assertRaises(HTTPError) as error:self.request('/api/data/job?id=other-business-job')
        self.assertEqual(403,error.exception.code)
        with self.assertRaises(HTTPError):self.request('/api/data/refresh',dict(dataset='orders',request_id='not-allowed-001'))

if __name__=='__main__':unittest.main()
