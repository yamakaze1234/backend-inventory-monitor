"""订单状态、发货看板与前台批次发布的隔离验证。"""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ops_front_publish import publish
from douyin_online import FIELD_NAMES

SOURCE = Path(__file__).resolve().parents[1] / 'integrations/order_backend/matching'


def load(name):
    spec = spec_from_file_location('fixture_' + name, SOURCE / (name + '.py'))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackendTests(unittest.TestCase):
    def test_sdk_timeline_queried_only_after_all_orders_pass_shop_check(self):
        bridge = load('order_status_bridge')
        client = Mock()
        client._paged.return_value = [
            {'shop_id':'76','c_billcode':'NO-001','c_platform_billcode':'00123A'},
            {'shop_id':'76','c_billcode':'NO-002','c_platform_billcode':'00123A-1'},
        ]
        def detail(code):
            return {'data_order':[dict(c_billcode=code, shop_id='76' if code == 'NO-002' else '99',
                                       c_platform_billcode='00123A-1')]}
        client.order_detail.side_effect = detail
        sdk = Mock()
        sdk._关联单号.return_value = (['NO-002', 'NO-001'], ['CK-001'], ['SF001'])
        sdk.网店订单操作日志查询.return_value = [{'步骤':'打包','操作人':None,'时间':None,'对应单号':'CK-001'}]
        audit_api = lambda *a, **kw: SimpleNamespace(base_url='fixture', params=lambda: {})
        result = bridge.query_verified_orders('出库单号','CK-001',['76'], connect=lambda shop: client, sdk=sdk, audit_api=audit_api)
        self.assertEqual(result['status'], 'unverified')
        sdk.网店订单操作日志查询.assert_not_called()
        result = bridge.query_verified_orders('网店单号','00123A-1',['76'], connect=lambda shop: client, sdk=sdk, audit_api=audit_api)
        self.assertEqual(result['orders'][0]['erp_no'], 'NO-002')
        self.assertEqual(result['orders'][0]['events'][0]['step'], '打包')
        self.assertEqual(client._paged.call_args.args[1]['search_billcode_filter'], '00123A-1')

    def test_board_includes_normal_orders_preserves_manual_fields_and_closes_all_parcels(self):
        board = load('ops_shipping_board')
        pending = [dict(shop_id='76', erp_no='NO-001', urgency='正常', note='不可透传')]
        remote = [dict(shop_id='76', erp_no='NO-002', record_id='tracked', lifecycle='in_progress',
                       note='人工备注', handling='处理中'),
                  dict(shop_id='76', erp_no='NO-003', record_id='tracked-2', lifecycle='in_progress')]
        def check(shop, code):
            return dict(route=dict(numbers=['SF1','SF2'], complete=True, phone_last_four='1234',
                                   evidence=[dict(number='SF1', collected=True), dict(number='SF2', collected=code == 'NO-002')]))
        changes = board.reconcile(pending, remote, batch_id='run', checked_at='2026-11-12', check=check, scoped={'76'}, complete=True)
        self.assertEqual(set(board.ROLES), {'客服','操作','库房打包','系统装机'})
        self.assertEqual([row['lifecycle'] for _, row in changes], ['in_progress','ended','in_progress'])
        self.assertEqual(changes[1][0], 'tracked')
        self.assertNotIn('note', changes[1][1])
        self.assertNotIn('1234', json.dumps(changes))
        incomplete = board.reconcile(pending, remote, batch_id='run', checked_at='2026-11-12', check=check, scoped={'76'}, complete=False)
        self.assertEqual(len(incomplete), 1)

    def test_front_publishes_only_verified_active_rows_and_checks_write_readback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fields = {name: name for name in FIELD_NAMES}
            target_fields = {k: k for k in ('scope','batch','checked_at','sync_state','payload')}
            target = dict(shop_ids=['76'], profile='profile', base_id='base', table_id='batch-table', fields=target_fields)
            (root/'ops-command-sources.json').write_text(json.dumps({'front_report_writer':target}), encoding='utf-8')
            config = dict(profile='profile', base_id='base', table_id='detail', fields=fields)
            rows = [{'cells': {'关联键':'store:1:2','商品ID':'1','SKU ID':'2','补库存标记':'待人工核对','平台库存':0}},
                    {'cells': {'关联键':'store:1:3','商品ID':'1','SKU ID':'3','补库存标记':'已退出提醒'}},
                    {'cells': {'关联键':'other:1:2','商品ID':'1','SKU ID':'2','补库存标记':'低库存','平台库存':9}}]
            table = Mock()
            saved = []
            table.query.side_effect = lambda: saved
            table.write.side_effect = lambda action, value: saved.extend(dict(r, recordId='fixture') for r in value)
            shop = dict(id='store', maintenance_shop_id='76')
            receipt = dict(ok=True, at=1794386360, fingerprint='batch')
            publish(root, shop, config, receipt, verified_rows=rows, client_factory=lambda cfg: table)
            payload = json.loads(saved[0]['cells']['payload'])
            self.assertEqual(len(payload['items']), 1)
            self.assertEqual(payload['items'][0]['platform_stock'], 0)
            self.assertEqual(payload['items'][0]['replenish_state'], '待人工核对')
            publish(root, shop, config, receipt, verified_rows=rows, client_factory=lambda cfg: table)
            table.write.assert_called_once()


if __name__ == '__main__':
    unittest.main()
