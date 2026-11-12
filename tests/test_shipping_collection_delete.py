"""验证揽收删除只针对已登记的在线行，未知结果不重放。"""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class CollectionDeleteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.number = 'SF1234567890123'
        self.other = 'SF1234567890124'
        self.key = '76:NO-fixture'
        self.row = dict(recordId='record-1', cells={'key':self.key})
        self.order = dict(shopId='76', code='NO-fixture', level='紧急')
        self.report = dict(orders=[self.order])
        config = dict(enabled=True, url='https://example.invalid/table', fields={'订单键':'key'})
        (self.root/'shipping-online.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
        self.table = Mock()
        self.table.records.return_value = [self.row]
        self.logistics = Mock()
        self.logistics.check.return_value = dict(collected=True, numbers=[self.number,self.other])
        self.logistics.parcels.return_value = ([self.number,self.other], True, None)
        imports = dict(shipping_monitor=SimpleNamespace(CN=timezone.utc,date=datetime.fromisoformat,identifier=str),
                       patrol_service=SimpleNamespace(single_instance=lambda path: nullcontext(True)),
                       group_order_bot=SimpleNamespace(safe_error=str))
        scope = patch.dict('sys.modules', imports)
        scope.start()
        self.addCleanup(scope.stop)
        path = Path(__file__).resolve().parents[1]/'integrations/order_backend/matching/shipping_online.py'
        self.module = runpy.run_path(str(path))

    def state(self, action=None, record_id='record-1'):
        saved = dict(orders={self.key:dict(recordId=record_id,order=self.order)},pending={self.key:action} if action else {})
        (self.root/'shipping-online-state.json').write_text(json.dumps(saved),encoding='utf-8')

    def sync(self):
        return self.module['sync'](self.report,self.root,lambda *_:self.table,lambda *_:self.logistics)

    def test_all_parcels_and_managed_record_are_rechecked_before_delete(self):
        self.state()
        result = self.sync()
        self.assertEqual(result['deleted'],1)
        self.table.delete.assert_called_once_with('record-1')
        self.logistics.parcels.assert_called_once_with('76','NO-fixture')
        audit = [json.loads(s) for s in (self.root/'shipping-collection-audit.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['action'] for r in audit],['delete_requested','delete_verified'])

    def test_new_or_changed_parcel_keeps_the_row(self):
        self.state()
        self.logistics.parcels.return_value = ([self.number],True,None)
        result = self.sync()
        self.assertFalse(result['complete'])
        self.table.delete.assert_not_called()

    def test_manual_recreated_row_with_same_key_is_not_deleted(self):
        self.state(record_id='replaced-record')
        result = self.sync()
        self.assertFalse(result['complete'])
        self.table.delete.assert_not_called()

    def test_unknown_delete_is_only_reconciled_by_readback(self):
        self.state('delete')
        self.assertFalse(self.sync()['complete'])
        self.table.delete.assert_not_called()
        self.table.write.assert_not_called()
        self.table.records.return_value = []
        self.assertTrue(self.sync()['complete'])
        state = json.loads((self.root/'shipping-online-state.json').read_text(encoding='utf-8'))
        self.assertEqual(state,dict(orders={},pending={}))
        self.table.delete.assert_not_called()
        self.table.write.assert_not_called()

    def test_mixed_package_status_does_not_confirm_collection(self):
        # 一张已揽收、另一张空轨迹时，整单仍保留。
        (self.root/'shipping-sf-auth.json').write_text('{"provider":"sf-sdk"}',encoding='utf-8')
        route_responses = {
            self.number: [dict(firstStatusName='已揽收',acceptTime='2020-01-01T00:00:00+00:00')],
            self.other: [],
        }
        def lookup(auth,number,phone):
            data = dict(success=True,errorCode='S0000',msgData=dict(routeResps=[dict(mailNo=number,routes=route_responses[number])]))
            return dict(ok=True,data=data)
        logistics = self.module['Logistics'](self.root,{})
        logistics.parcels = Mock(return_value=([self.number,self.other],True,None))
        with patch.dict('sys.modules',shipping_sf_route=SimpleNamespace(query_route=lookup)):
            self.assertFalse(logistics.check('76','NO-fixture')['collected'])
            route_responses[self.other] = route_responses[self.number]
            self.assertTrue(logistics.check('76','NO-fixture')['collected'])
