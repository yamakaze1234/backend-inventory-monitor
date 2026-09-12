import json
import tempfile
import unittest
import urllib.error
import urllib.request
from inventory_monitor import InventoryService
from inventory_bridge import start_bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = InventoryService(self.temp.name)
        self.s.save_product(dict(sku='A', name='内存', category='内存', threshold=10))
        self.server = start_bridge(self.s, port=0, live_delivery=False)
        self.addCleanup(self.server.close)

    def post(self, path, body, origin='https://cqzs.3cerp.com', client=True):
        headers = {'Content-Type':'application/json', 'Origin':origin}
        if client: headers['X-Inventory-Client']='erp-userscript-v1'
        req = urllib.request.Request(self.server.url+path, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req) as r: return json.load(r)

    def poll(self, client='tab1'):
        return self.post('/poll', {'client_id':client, 'scope':'account:2', 'warehouse':'公司大库'})

    def test_only_verified_origin_and_custom_header(self):
        for origin, client in [('https://evil.invalid', True), ('null', True), ('https://cqzs.3cerp.com', False)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/poll', {}, origin, client)
            self.assertEqual(error.exception.code, 403)

    def test_lease_serializes_tabs_and_complete_snapshot(self):
        lease = self.poll()
        self.assertTrue(lease['check_id'])
        self.assertFalse(self.poll('tab2').get('check_id'))
        body = dict(check_id=lease['check_id'], client_id='tab1', scope='account:2',
                    warehouse='公司大库', complete=True, rows=[dict(sku='A',name='内存',able=2,purchase=0,stock=2)])
        self.post('/snapshot', body)
        self.assertEqual(self.s.status()['products'][0]['able'], 2)
        self.assertFalse(self.poll().get('check_id'))
        with self.assertRaises(urllib.error.HTTPError): self.post('/snapshot', body)

    def test_stale_or_wrong_lease_rejected(self):
        self.poll()
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/snapshot', dict(check_id='wrong',client_id='tab2',scope='account:2',rows=[]))

    def test_pause_prevents_poll_and_pending_acceptance(self):
        lease = self.poll()
        self.s.set_enabled(False)
        self.assertFalse(self.poll().get('check_id'))
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/snapshot', dict(check_id=lease['check_id'],client_id='tab1',scope='account:2',
                                      warehouse='公司大库',complete=True,rows=[]))

    def test_heartbeat_does_not_clear_collection_error(self):
        self.poll()
        self.post('/error',dict(client_id='tab1'))
        self.poll()
        self.assertEqual(self.s.status()['connection'],'error')

    def test_warehouse_lookup_runs_while_monitoring_paused_and_is_correlated(self):
        self.s.accept_snapshot('catalog', [dict(sku='A', goods_id='88102', name='显卡', able=0,stock=0,purchase=0)], scope='account:2')
        request = self.s.request_warehouses('A', '88102')
        self.s.set_enabled(False)
        base = dict(client_id='tab1', scope='account:2', warehouse='公司大库', capabilities=['warehouse_details_v1'])
        lease = self.post('/poll', base)
        self.assertEqual(lease.get('job_type'), 'warehouse_lookup')
        details = dict(goods_id='88102', complete=True, depots=[dict(id='131',name='稀缺货源',stock=20,able=None,purchase=None)])
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/warehouse-result', dict(base,check_id=lease['check_id'],request_id='wrong',details=details))
        self.post('/warehouse-result', dict(base,check_id=lease['check_id'],request_id=request['request_id'],details=details))
        self.assertEqual(self.s.warehouse_options('88102')['depots'][0]['stock'], 20)
        self.assertFalse(self.s.status()['enabled'])
        self.assertFalse(self.poll().get('check_id'))

    def test_source_change_during_collection_discards_old_job_and_rechecks_immediately(self):
        row = dict(sku='A', goods_id='88102', name='显卡', able=0,stock=0,purchase=0)
        self.s.accept_snapshot('catalog', [row], scope='account:2')
        req = self.s.request_warehouses('A', '88102')
        self.s.accept_warehouses(req['request_id'], dict(goods_id='88102',complete=True,
            depots=[dict(id='131',name='稀缺货源',stock=20)]), scope='account:2')
        self.s.request_check()
        old = self.poll()
        p = dict(sku='A',name='显卡',goods_id='88102',threshold=5,stock_basis='warehouse_stock',warehouse_id='131',warehouse_name='稀缺货源')
        self.s.save_product(p)
        base = dict(client_id='tab1',scope='account:2',warehouse='公司大库',capabilities=['warehouse_details_v1'])
        result = self.post('/snapshot', dict(base,check_id=old['check_id'],complete=True,rows=[row],journals={'A':[]}))
        self.assertTrue(result.get('retry'))
        new = self.post('/poll', base)
        self.assertTrue(new.get('check_id'))
        self.assertEqual(new['warehouse_products'][0]['warehouse_id'],'131')
        self.s.save_product(dict(p,stock_basis='company_able'))
        reverse = self.post('/snapshot',dict(base,check_id=new['check_id'],complete=True,rows=[row],journals={}))
        self.assertTrue(reverse.get('retry'))
        self.assertTrue(self.post('/poll',base).get('check_id'))


if __name__ == '__main__': unittest.main()
