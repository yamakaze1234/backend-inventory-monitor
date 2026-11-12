"""使用本机回环、临时数据库和内存外部来源验证运营指令。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from inventory_console import InventoryConsole
from inventory_monitor import InventoryService
from ops_command_api import CommandAPI, CommandError, Access
from ops_commands import COMMANDS
from ops_inventory import InventoryCommands, search
from ops_receiver import LocalCommandClient, Receiver, VerifiedMention
from ops_replies import render
from ops_runtime import CommandBusiness
from ops_sources import ReportTables, order_status
from ops_settings import save as save_group


class FlowTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.service = InventoryService(self.root)
        self.cost = Mock(return_value=dict(costs={'56281': 36500}, collectedAt=1794386360))
        self.inventory = InventoryCommands(self.service, cost=self.cost)
        self.order = Mock()
        self.order.snapshot.return_value = dict(workerOnline=True, serviceEnabled=True, shops=[{'id':'76'}])
        self.order.action.return_value = dict(jobId='task-1', status='queued')
        self.order.ops_task.return_value = dict(status='completed', task_id='task-1', shop_id='76',
                                                final_reply_owned=True, success=1, skipped=0, review=0, unit='单')
        self.business = CommandBusiness(self.inventory, order=self.order, shops=lambda: {'76':'样例店'})
        self.api = CommandAPI(self.service, self.business, 'robot', 'x' * 40)
        self.actor = dict(org_id='org', group_id='group', user_id='member')
        self.features = list(COMMANDS)
        self.authorize(self.features, fields=['erp_cost'])
        now = time.time()
        self.rows = [
            dict(goods_id='56281', sku='GPU-W8', name='显卡 5060 Ti 白色 8G', stock=0, able=0, purchase=0, erp_able=11, pending_review=11),
            dict(goods_id='56282', sku='GPU-W16', name='显卡 5060Ti 白色 16G', stock=5, able=5, purchase=0),
            dict(goods_id='56283', sku='GPU-W48', name='显卡 5060Ti 白色 48G', stock=3, able=3, purchase=0),
        ]
        self.service.accept_snapshot('sample', self.rows, scope='isolated', observed_at=now)

    def authorize(self, selected, *, fields=(), shops=('76',)):
        self.api.groups.save('org', 'robot', 'group', selected,
                             members={'member':dict(commands=self.features, shops=list(shops), fields=list(fields))})

    def request(self, cmd, args=None, ident='a', actor=None):
        return self.api.route('POST', '/api/commands/v1/requests', {'Authorization':'Bearer ' + 'x' * 40},
                              dict(request_id='dingtalk:'+ident, actor=actor or self.actor, command=cmd, args=args or {}))

    def test_help_permissions_and_price_projection_on_replay(self):
        status, reply = self.request('inventory.get', {'goods_id':'56281'})
        self.assertEqual(status, 200)
        self.assertEqual(reply['data']['items'][0]['qty'], 0)
        self.assertIn('实际可销 0 件（ERP 可销 11 件，待审 11 件）', render(reply))
        self.assertIn('¥365.00', render(reply))
        self.authorize(['help', 'inventory.get'])
        _, replay = self.request('inventory.get', {'goods_id':'56281'})
        self.assertTrue(replay['replayed'])
        self.assertIsNone(replay['data']['items'][0]['erp_unit_cost_cents'])
        self.cost.assert_called_once()
        _, help_reply = self.request('help', ident='help')
        self.assertNotIn('添加监控', render(help_reply))
        self.assertEqual(self.request('monitor.add', {'goods_id':'56281', 'threshold':0}, 'b')[1]['code'], 'COMMAND_DISABLED')
        self.assertEqual(self.request('help', ident='c', actor=dict(self.actor, user_id='other'))[1]['code'], 'FORBIDDEN')
        _, unknown = self.request('inventory.get', {'goods_id':'56281', 'warehouse_id':'no-stock'}, 'd')
        self.assertIsNone(unknown['data']['items'][0]['qty'])

    def test_latest_completed_price_batch_and_workbook_price_scope(self):
        from tests.test_profit_monitor import manifest, FakeSource
        from erp_price_monitor import ProfitMonitor
        from profit_manifest import canonical
        from openpyxl import Workbook

        source = FakeSource()
        monitor = ProfitMonitor(self.root, source_provider=lambda: source)
        self.addCleanup(monitor.stop)
        path = self.root/'manifest.json'
        path.write_text(canonical(manifest()), encoding='utf-8')
        monitor.bind(path)
        monitor.check(allow_notifications=False)
        source.cost += 123
        batch = monitor.check(allow_notifications=False)
        with monitor.db() as db:
            db.execute("INSERT INTO runs(id,kind,started,status) VALUES('unfinished','price-v2',?,'running')", (time.time() + 60,))
        _, reply = self.request('price.changes', ident='price')
        self.assertEqual(reply['data']['batch_id'], batch)
        self.assertEqual(reply['data']['items'][0]['delta_cents'], 123)

        bindings = []
        for shop, price in [('76', 6999), ('99', 8999)]:
            book = Workbook()
            sheet = book.active
            sheet.title = '综合表'
            sheet.append(['SPU', 'SKU', '简称', 'CPU_id', 'CPU', 'CPU_数量', '机箱_id', '机箱', '机箱_数量', '到手价'])
            sheet.append(['1234567890123456789', '2234567890123456789', '配置', 56281, '配件', 1, 99, '机箱', 1, price])
            settings = book.create_sheet('禁止编辑')
            settings['G2'] = int(shop)
            path = self.root/(shop + '.xlsx')
            book.save(path)
            book.close()
            bindings.append(dict(path=str(path), sheet='综合表', shop_id=shop, host_price_column='到手价'))
        self.inventory.maintenance = bindings
        self.authorize(self.features, shops=('76', '99'), fields=['maintenance_price'])
        _, reply = self.request('inventory.get', {'goods_id':'56281'}, 'maintenance')
        self.assertEqual([p['price_cents'] for p in reply['data']['items'][0]['maintenance_prices']], [699900, 899900])
        self.authorize(self.features, shops=('76',), fields=['maintenance_price'])
        _, one_shop = self.request('inventory.get', {'goods_id':'56281'}, 'new')
        self.assertEqual([p['price_cents'] for p in one_shop['data']['items'][0]['maintenance_prices']], [699900])
        _, replay = self.request('inventory.get', {'goods_id':'56281'}, 'maintenance')
        self.assertEqual(replay['data']['items'][0]['maintenance_prices'], [])

    def test_monitor_transaction_duplicate_and_restart(self):
        payload = {'goods_id':'56281', 'threshold':5}
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda i: self.request('monitor.add', payload, str(i))[1]['data']['outcome'], range(4)))
        self.assertEqual(results.count('created'), 1)
        self.assertEqual(results.count('already_exists'), 3)
        self.assertEqual(len(self.service.products()), 1)
        self.api = CommandAPI(self.service, self.business, 'robot', 'x' * 40)
        self.assertTrue(self.request('monitor.add', payload, '0')[1]['replayed'])
        self.assertEqual(self.request('monitor.add', dict(payload, threshold=8), '0')[1]['code'], 'IDEMPOTENCY_CONFLICT')
        original = self.service.products()[0]
        _, reply = self.request('monitor.threshold.set', dict(payload, threshold=0), 'zero')
        self.assertEqual(reply['data']['previous_threshold'], 5)
        self.assertEqual(self.service.products()[0], dict(original, threshold=0))
        self.assertEqual(self.request('monitor.threshold.set', dict(payload, threshold=0), 'same')[1]['data']['outcome'], 'unchanged')
        self.assertEqual(self.request('monitor.threshold.set', dict(payload, threshold=True), 'invalid')[0], 400)

    def test_group_settings_save_readback_and_separate_bindings(self):
        values = dict(org_id='org', robot_id='robot', group_id='group', name='样例群', help=['help'],
                      inventory=['inventory.get'], monitor=[], orders=[],
                      members=[dict(user_id='member', commands='help,inventory.get', shops='76', fields='')])
        self.assertEqual(save_group(self.service, values), ['help', 'inventory.get'])
        save_group(self.service, dict(values, group_id='other', inventory=[]))
        self.assertEqual(CommandAPI(self.service, self.business, 'robot', 'x' * 40).groups.get('org','robot','group'),
                         ['help','inventory.get'])
        self.assertEqual(self.api.groups.get('org','robot','other'), ['help'])

    def test_http_receiver_one_initial_and_one_final(self):
        assets = self.root / 'assets'
        assets.mkdir()
        (assets / 'index.html').write_text('test', encoding='utf-8')
        console = InventoryConsole(self.service, assets=assets, bot=self.order, commands=self.api)
        self.addCleanup(console.close)
        endpoint = console.url + '/api/commands/v1/capabilities'
        for headers in ({'X-Console-Token':console.token}, {'Authorization':'Bearer ' + 'x'*40,'Origin':'http://other'}):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(endpoint, headers=headers))
            self.assertEqual(error.exception.code, 403)
        client = LocalCommandClient(console.url, 'x' * 40)
        self.assertEqual(client.request('capabilities')['version'], 'commands/v1')
        sent = []
        receiver = Receiver(client, 'robot', self.root/'replies.sqlite3', lambda event, text: (sent.append(text), 'sent')[1])
        mention = VerifiedMention('org','group','member','robot','message','匹配订单 样例店 123456789012345A-1')
        self.assertIsNone(receiver.receive('伪造的 @'))
        self.assertEqual(receiver.receive(mention)['status'], 'queued')
        receiver.receive(mention)
        self.assertEqual(len(sent), 1)
        self.authorize(['task.status'])
        # 新实例恢复同一持久账本，最终回执仍仅投递一次。
        restarted = Receiver(client, 'robot', self.root/'replies.sqlite3', lambda event, text: (sent.append(text), 'sent')[1])
        restarted.tick()
        restarted.tick()
        self.assertEqual(len(sent), 2)
        self.assertIn('已匹配并回读确认', sent[-1])
        self.assertEqual(client.request('requests/' + mention.receipt, actor=mention.actor)['delivery_status'], 'sent')
        self.order.action.assert_called_once()

    def test_ai_is_only_fallback_and_rechecks_specs(self):
        ai = Mock(return_value={'candidates':['5060 Ti 白色'], 'conditions':['8G']})
        found, mode, _, _ = search(self.rows, {'keyword':'５０６０ Ti 白色 8GB'}, ai)
        self.assertEqual(([r['goods_id'] for r in found], mode), (['56281'], 'keyword'))
        ai.assert_not_called()
        found, mode, _, _ = search(self.rows, {'keyword':'白色的五零六零钛 8G'}, ai)
        self.assertEqual(([r['goods_id'] for r in found], mode), (['56281'], 'ai_assisted'))
        found, _, _, choices = search(self.rows, {'keyword':'白色的五零六零钛 12G'}, ai)
        self.assertFalse(found)
        self.assertTrue(choices)
        ai.side_effect = TimeoutError()
        self.assertFalse(search(self.rows, {'keyword':'白色的五零六零钛 12G'}, ai)[0])


class SourceTests(unittest.TestCase):
    def test_order_scope_and_latest_verified_time(self):
        events = [dict(step='打包', occurred_at='2026-11-12 09:50', operator='人员', reference_number='CK-001'),
                  dict(step='打包', occurred_at='2026-11-12 08:50'), dict(step='装机', occurred_at='未知')]
        orders = [dict(shop_id='76', erp_no='NO-001', web_order_numbers=['001A-1'], events=events)]
        read = Mock(return_value=dict(status='ok', orders=orders))
        access = Access(frozenset({'76'}), frozenset(), ())
        data = order_status(read, {'number':'001A-1'}, access)
        read.assert_called_once_with('网店单号','001A-1',['76'])
        self.assertEqual(len(data['events']), 3)
        self.assertIsNone(data['events'][-1]['occurred_at'])
        self.assertIsNone(data['latest_events'][0]['operator'])
        self.assertEqual(data['latest_events'][0]['reference_number'], 'CK-001')
        orders.append(dict(shop_id='99', erp_no='NO-002', events=[]))
        with self.assertRaises(CommandError) as error:
            order_status(read, {'number':'CK-001','number_type':'outbound_no'}, access)
        self.assertEqual(error.exception.http, 403)

    def test_report_fallback_and_different_batches_do_not_merge(self):
        def batch(id, at, counts, state='verified', items=()):
            return dict(scope='76', batch=id, checked_at=at, sync_state=state,
                        payload=json.dumps(dict(complete=True, counts=counts, items=items)))
        rows = {'orders': [batch('new', '2026-11-12 11:00', {}, 'unknown'),
                           batch('old', '2026-11-12 10:00', dict(pending_scanned=0,overdue=0,due_within_48h=0,deadline_unknown=0))],
                'shortages':[batch('other', '2026-11-12 09:00', dict(shortage_goods=2,inventory_unknown_goods=1))],
                'front':[batch('front', '2026-11-12 10:20', {}, items=[
                    dict(shop_id='76',spu='1',sku_id='2',active=True,platform_stock=0),
                    dict(shop_id='99',spu='1',sku_id='2',active=True,platform_stock=9)])]}
        bindings = {name:dict(alias=alias, shop_ids=['76']) for name,alias in
                    [('shipping_orders','orders'),('shipping_shortages','shortages'),('front_stock','front')]}
        reader = ReportTables(bindings, lambda b, s: (rows[b['alias']], True))
        report = reader.shipping({'76'})
        self.assertEqual(report['outcome'], 'partial')
        self.assertIsNone(report['counts']['pending_scanned'])
        self.assertEqual(report['tables'][0]['counts']['pending_scanned'], 0)
        self.assertEqual(report['tables'][0]['sync_state'], 'unknown')
        self.assertEqual(reader.front('76', {})['items'][0]['platform_stock'], 0)
        self.assertEqual(reader.front('76', {'sku_id':'absent'})['outcome'], 'no_report')


if __name__ == '__main__':
    unittest.main()
