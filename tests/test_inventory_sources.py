import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from inventory_sources import InventoryRuntimeService
from inventory_bridge import Bridge
from inventory_monitor import InventoryService
from inventory_connection_alert import run_once as connection_alert
from inventory_delivery import DeliveryWorker, format_events
from sql_inventory_source import warehouse_key


SETTINGS=dict(server='offline-test',port=1433,database='ERP',user='fixture')

def raw(stock=8,pending=0):
    return dict(goods_id='101',商品编码='SKU101',商品名称='测试商品',库房='公司大库',分库数=stock,分库可销数=stock,分库待入=pending)

class Connector:
    def __init__(self): self.rows=[raw()]; self.closed=0; self.queries=[]; self.fail=False
    def __call__(self,**kwargs): return self
    def cursor(self):return self
    def execute(self,query,params=None):
        self.queries.append(query)
        if self.fail:raise RuntimeError('private test-only diagnostic')
        self.batch=copy.deepcopy(self.rows) if '分库库存' in query else [dict(goods_id='101',商品编码='SKU101',商品名称='测试商品',总数量=8,可销数=8,待入=0)]
    def fetchmany(self,n):
        rows,self.batch=self.batch,[];return rows
    def close(self):self.closed+=1

class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service=InventoryRuntimeService(self.temp.name);self.connector=Connector();self.now=time.time()

    def connect(self):
        self.service.configure_sql(SETTINGS,'fixture-only',connector=self.connector)
        self.service.sql_worker.step(self.now)

    def watch(self,**kwargs):
        return self.service.save_product(dict(sku='SKU101',goods_id='101',name='测试商品',threshold=0,**kwargs))

    def poll(self):
        self.service.request_check();self.now+=31;self.service.sql_worker.step(self.now)

    def test_default_sql_old_cache_does_not_mean_connected(self):
        self.assertEqual(self.service.source_mode(),'sql')
        self.connect();self.assertEqual(self.service.status()['connection'],'online')
        restarted=InventoryRuntimeService(self.temp.name)
        self.assertEqual(restarted.status()['connection'],'waiting')

    def test_sql_events_use_existing_delivery_not_preview(self):
        self.connect();self.watch();self.poll()
        self.connector.rows[0].update(分库数=12,分库可销数=12,分库待入=5);self.poll()
        events=self.service.status()['events']
        self.assertEqual({e['kind'] for e in events},{'stock_increase','pending_inbound'})
        self.assertTrue(all(e['delivery_status']=='pending' for e in events))
        calls=[]
        worker=DeliveryWorker(self.service,lambda body:calls.append(body) or {'status':'sent'})
        worker.run_once();worker.run_once()
        self.assertEqual(len(calls),2)  # Existing broadcaster groups by cause.
        self.assertTrue(all(e['delivery_status']=='sent' for e in self.service.status()['events']))
        self.poll();self.assertEqual(len(self.service.status()['events']),2)

    def test_sql_mode_ignores_browser_poll_error_and_recovery(self):
        self.connect();bridge=Bridge(self.service,port=0);self.addCleanup(bridge.close)
        self.assertIsNone(bridge.handle('/poll',{})['check_id'])
        self.assertFalse(bridge.handle('/recovery-config',{})['enabled'])
        bridge.handle('/error',{})
        self.assertEqual(self.service.status()['connection'],'online')
        with self.assertRaises(ValueError):bridge.handle('/snapshot',{})
        from inventory_browser_recovery import BrowserRecovery
        cli=Mock();BrowserRecovery(self.service,cli).run_once();cli.run.assert_not_called()

    def test_existing_script_watch_rebases_without_changing_robot_meta_or_name(self):
        original=InventoryService(self.temp.name)
        original.save_product(dict(sku='SKU101',goods_id='101',name='自定义名称',threshold=10))
        original.accept_snapshot('script-before',[dict(sku='SKU101',goods_id='101',name='测试商品',able=20,stock=20,purchase=0)],scope='script:2')
        with original.db() as db:original.put(db,'daily_schedule_fixture',{'slots':['10:00']})
        self.connector.rows[0]=raw(100,50);self.connect()
        self.assertFalse(self.service.status()['events'])
        self.assertEqual(self.service.products()[0]['name'],'自定义名称')
        with self.service.db() as db:
            self.assertEqual(self.service.get(db,'daily_schedule_fixture'),{'slots':['10:00']})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_snapshot_archive').fetchone()[0],1)
        self.connector.rows[0]=raw(103,50);self.poll()
        self.assertEqual(self.service.status()['events'][0]['kind'],'stock_increase')
        self.service.use_script()
        self.service.accept_snapshot('script-after',[dict(sku='SKU101',goods_id='101',name='测试商品',able=200,stock=200,purchase=8)],scope='script:2')
        self.assertEqual(len(self.service.status()['events']),1)

    def test_existing_numeric_warehouse_watch_survives_sql_switch(self):
        original=InventoryService(self.temp.name)
        row=dict(sku='SKU101',goods_id='101',name='测试商品',able=8,stock=8,purchase=0)
        original.accept_snapshot('old',[row],scope='script:2')
        request=original.request_warehouses('SKU101','101')
        original.accept_warehouses(request['request_id'],dict(goods_id='101',complete=True,depots=[dict(id='125',name='样品库',stock=4,able=4,purchase=0)]),scope='script:2')
        original.save_product(dict(row,threshold=0,stock_basis='warehouse_stock',warehouse_id='125',warehouse_name='样品库'))
        self.connector.rows.append(dict(raw(4),库房='样品库'));self.connect()
        self.assertEqual(self.service.status()['products'][0]['monitor_qty'],4)
        self.assertEqual(self.service.products()[0]['warehouse_id'],'125')
        self.connector.rows[-1]['分库数']=9;self.poll()
        self.assertEqual(self.service.status()['events'][0]['warehouse'],'样品库')

    def test_new_sql_warehouse_rebinds_by_exact_name_on_script_switch(self):
        self.connect();self.watch(stock_basis='warehouse_stock',warehouse_id=warehouse_key('公司大库'),warehouse_name='公司大库')
        self.service.use_script()
        bridge=Bridge(self.service,port=0);self.addCleanup(bridge.close)
        body=dict(client_id='test',scope='script:2',warehouse='公司大库',capabilities=['warehouse_details_v1'])
        task=bridge.handle('/poll',body)
        self.assertEqual(task['job_type'],'warehouse_lookup')
        details=dict(goods_id='101',complete=True,depots=[dict(id='2',name='公司大库',stock=8,able=8,purchase=0)])
        bridge.handle('/warehouse-result',dict(body,check_id=task['check_id'],request_id=task['request_id'],details=details))
        self.assertEqual(self.service.products()[0]['warehouse_id'],'2')

    def test_inactive_sql_worker_never_queries_and_switch_while_busy_rejected(self):
        self.connect();n=len(self.connector.queries);self.service.use_script();self.poll()
        self.assertEqual(len(self.connector.queries),n)
        self.service.sql_worker.busy=True
        with self.assertRaises(ValueError):self.service.configure_sql(SETTINGS,'fixture-only',connector=self.connector)

    def test_sql_interval_does_not_generate_browser_disconnect_alert(self):
        self.connect();self.watch();sender=Mock();sender.send.return_value={'status':'sent'}
        with self.service.db() as db:heartbeat=self.service.get(db,'heartbeat')
        self.assertFalse(connection_alert(self.service,sender,now=heartbeat+300))
        self.assertFalse(connection_alert(self.service,sender,now=heartbeat+7210))
        self.assertFalse(connection_alert(self.service,sender,now=heartbeat+7300))
        self.assertTrue(connection_alert(self.service,sender,now=heartbeat+7610))
        self.assertNotIn('浏览器',sender.send.call_args.args[2])
        sender.send.assert_called_once()

    def test_history_filters_full_history_before_limit_and_never_requeues(self):
        with self.service.db() as db:
            for i in range(220):
                event=dict(id=str(i),sku='S',name='商品',kind='pending_inbound' if i<3 else 'low',after={'goods_id':'101'},warehouse='京仓')
                db.execute('INSERT INTO events(id,data,created,status) VALUES(?,?,?,?)',(str(i),json.dumps(event),i+1,'sent'))
        events,total=self.service.preview_events(kinds={'pending_inbound'},warehouse='京仓',query='101')
        self.assertEqual(total,3);self.assertEqual(len(events),3)
        self.assertTrue(all(e['delivery_status']=='sent' for e in events))
        self.assertEqual(self.service.preview_events(since=220)[1],1)

    def test_remember_password_is_encrypted_and_reused_only_for_same_settings(self):
        with patch('monitor_secret_store.protect',return_value=b'ciphertext'),patch('monitor_secret_store.unprotect',return_value=b'fixture-only'):
            self.service.configure_sql(SETTINGS,'fixture-only',remember=True,connector=self.connector)
            self.assertEqual((Path(self.temp.name)/'sql_connection.dat').read_bytes(),b'ciphertext')
            self.service.configure_sql(SETTINGS,'',remember=True,connector=self.connector)
            with self.assertRaises(ValueError):self.service.configure_sql(dict(SETTINGS,user='different'),'',remember=True,connector=self.connector)
        with self.service.db() as db:
            self.assertNotIn('fixture-only',str(db.execute('SELECT data FROM meta').fetchall()))


if __name__=='__main__':unittest.main()
