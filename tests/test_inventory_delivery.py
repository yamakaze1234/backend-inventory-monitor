import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory_monitor import InventoryService
from inventory_delivery import DeliveryWorker, DwsSender, resolve_destination, format_events, start_delivery
from suhao_cost_monitor import process_lines


class FakeSetup:
    def profiles(self):
        return [dict(key='corp:user', current=True)]

    def cli(self, args, profile=None):
        assert profile == 'corp:user'
        return dict(complete=True, chats=[dict(title='示例库存通知群', openConversationId='cid-real')])


class DeliveryTests(unittest.TestCase):
    def test_runtime_uses_robot_not_user_sender(self):
        called = threading.Event()
        def robot(service):
            called.set()
            raise ValueError('not configured')
        with patch('inventory_delivery.RobotSender', side_effect=robot), patch('inventory_delivery.DwsSender') as personal:
            handle = start_delivery(self.svc, live=True)
            try:
                self.assertTrue(called.wait(2))
                personal.assert_not_called()
            finally:
                handle.close()

    def test_robot_receipt_and_unknown_result(self):
        from inventory_delivery import RobotSender
        from unittest.mock import MagicMock
        (self.svc.root / 'inventory_robot.dat').write_bytes(b'test encrypted placeholder')
        sender = RobotSender(self.svc)
        for result, expected in [({'errcode': 0}, 'sent'), ({'errcode': 310000}, 'failed'), ({}, 'unknown')]:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(result).encode()
            with patch('monitor_secret_store.unprotect', return_value=b'https://oapi.dingtalk.com/robot/send?access_token=TEST_ONLY'), patch('urllib.request.urlopen', return_value=response) as request:
                self.assertEqual(sender('库存测试')['status'], expected)
                self.assertEqual(json.loads(request.call_args.args[0].data)['msgtype'], 'markdown')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = InventoryService(self.tmp.name)

    def add_event(self, ident, kind='low', observed=1700000000):
        event = dict(id=ident, sku=ident, name='测试产品', kind=kind, observed_at=observed,
                     cause='inventory_changed', threshold=10, after=dict(able=2, purchase=3, stock=4),
                     before=None, warehouse='公司大库')
        with self.svc.db() as db:
            db.execute('INSERT INTO events (id,data,created) VALUES (?,?,?)', (ident, json.dumps(event), time.time()))
        return event

    def state(self, ident):
        with self.svc.db() as db:
            return db.execute('SELECT status,attempts,retry_at FROM events WHERE id=?', (ident,)).fetchone()

    def test_destination_requires_unique_complete_current_profile(self):
        result = resolve_destination(self.svc, FakeSetup())
        self.assertEqual(result['group_id'], 'cid-real')
        with patch.object(FakeSetup, 'profiles', return_value=[]):
            with self.assertRaises(ValueError):
                resolve_destination(self.svc, FakeSetup())
        with patch.object(FakeSetup, 'cli', return_value=dict(complete=True, chats=[dict(title='示例库存通知群', openConversationId='a'), dict(title='示例库存通知群', openConversationId='b')])):
            with self.assertRaises(ValueError):
                resolve_destination(self.svc, FakeSetup())
        with patch.object(FakeSetup, 'cli', return_value=dict(complete=False, chats=[])):
            with self.assertRaises(ValueError):
                resolve_destination(self.svc, FakeSetup())

    def test_groups_same_round_sends_once(self):
        self.add_event('a'); self.add_event('b', 'oversold'); self.add_event('c', observed=1700007200)
        sent = []
        worker = DeliveryWorker(self.svc, sender=lambda body: sent.append(body) or {'status': 'sent'})
        worker.run_once(); worker.run_once(); worker.run_once()
        self.assertEqual(len(sent), 2)
        self.assertLess(sent[0].index('超售'), sent[0].index('低库存'))
        self.assertEqual(self.state('a')[:2], ('sent', 1))

    def test_unknown_is_not_retried(self):
        self.add_event('a')
        calls = []
        worker = DeliveryWorker(self.svc, sender=lambda body: calls.append(body) or {'status':'unknown'})
        worker.run_once(); worker.run_once(now=time.time()+10000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state('a')[0], 'unknown')

    def test_explicit_failure_backs_off_and_retries(self):
        self.add_event('a')
        worker = DeliveryWorker(self.svc, sender=lambda body: {'status':'failed'})
        now = time.time(); worker.run_once(now=now)
        self.assertGreater(self.state('a')[2], now)
        worker.run_once(now=now+1)
        self.assertEqual(self.state('a')[1], 1)
        worker.sender = lambda body: {'status':'sent'}
        worker.run_once(now=now+10000)
        self.assertEqual(self.state('a')[:2], ('sent', 2))

    def test_transport_exception_is_unknown(self):
        self.add_event('a')
        def timeout(body):
            raise TimeoutError('no result')
        DeliveryWorker(self.svc, sender=timeout).run_once()
        self.assertEqual(self.state('a')[0], 'unknown')

    def test_definitive_cli_rejection_is_failed_without_unapproved_retry(self):
        from inventory_delivery import DwsCommandError
        sender = DwsSender(self.svc, FakeSetup())
        with patch.object(sender.setup, 'cli', side_effect=DwsCommandError({'category':'validation', 'retryable':False})):
            result = sender('正文')
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['retryable'])
        self.add_event('a')
        worker = DeliveryWorker(self.svc, sender=lambda body: result)
        worker.run_once(); worker.run_once(now=time.time()+99999999)
        self.assertEqual(self.state('a')[:2], ('failed', 1))

    def test_unclassified_cli_failure_stays_unknown(self):
        from inventory_delivery import DwsCommandError
        sender = DwsSender(self.svc, FakeSetup())
        with patch.object(sender.setup, 'cli', side_effect=DwsCommandError({'category':'internal', 'retryable':True})):
            self.assertEqual(sender('正文')['status'], 'unknown')

    def test_packaged_cli_and_safe_structured_rejection(self):
        import sys
        from types import SimpleNamespace
        from inventory_delivery import delivery_setup, DwsCommandError
        executable = Path(self.tmp.name)/'dws.exe'
        executable.write_text('fixture executable is never run', encoding='utf-8')
        with patch.object(sys, '_MEIPASS', self.tmp.name, create=True), patch.object(sys, 'frozen', True, create=True), patch('monitor_portable_install._local_root', return_value=Path(self.tmp.name)/'local'):
            setup = delivery_setup(self.svc)
        # The bundled CLI must survive PyInstaller's temporary extraction folder.
        self.assertTrue(Path(setup.dws_path).is_relative_to(Path(self.tmp.name)/'local'/'runtime'))
        self.assertEqual(Path(setup.dws_path).read_bytes(), executable.read_bytes())
        self.assertTrue(setup.portable)
        response = SimpleNamespace(returncode=3, stdout='', stderr=json.dumps({'error':{
            'category':'validation', 'retryable':False, 'message':'sensitive diagnostic never retained'}}))
        with patch('inventory_delivery.subprocess.run', return_value=response):
            with self.assertRaises(DwsCommandError) as failure:
                setup.cli(['chat', '+send-to-group'], profile='corp:user')
        self.assertEqual(failure.exception.category, 'validation')
        self.assertNotIn('sensitive', str(failure.exception))

    def test_cli_explicit_retry_hint_is_bounded(self):
        self.add_event('a')
        worker = DeliveryWorker(self.svc, sender=lambda body: {
            'status':'failed', 'retryable':True, 'max_attempts':2, 'retry_after':90})
        now = time.time()
        worker.run_once(now=now)
        self.assertEqual(self.state('a')[2], now+90)
        worker.run_once(now=now+91)
        worker.run_once(now=now+999999)
        self.assertEqual(self.state('a')[:2], ('failed', 2))

    def test_unknown_delivery_remains_visible_after_idle_tick(self):
        self.add_event('a')
        worker = DeliveryWorker(self.svc, sender=lambda body: {'status':'unknown'})
        worker.run_once(); worker.run_once()
        with self.svc.db() as db:
            self.assertIn('待核实', self.svc.get(db, 'delivery_error'))
            self.assertEqual(self.svc.get(db, 'delivery_counts')['unknown'], 1)

    def test_inbound_formatter_shows_original_bill_quantity_and_receipt_time(self):
        event = self.add_event('receipt', kind='inbound')
        event['after']['inbound'] = dict(bill_code='RK-original', kind='采购入库', in_qty=8, created_at=1699990000)
        body = format_events([event], self.svc, now=1700000000)
        self.assertIn('RK-original', body)
        self.assertIn('入库数量：8', body)
        self.assertIn('2023-11-15 03:26:40', body)
        self.assertIn('采购入库', body)

    def test_cli_sender_checks_receipt_for_exact_destination(self):
        setup = FakeSetup()
        sender = DwsSender(self.svc, setup)
        with patch.object(setup, 'cli', side_effect=[{'openTaskId':'task-original'},
                {'openMessageId':'message-original', 'openConversationId':'cid-real'}]) as cli:
            result = sender('库存事件正文')
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['openTaskId'], 'task-original')
        self.assertEqual(cli.call_args_list[0].kwargs['profile'], 'corp:user')
        self.assertIn('cid-real', cli.call_args_list[0].args[0])
        with patch.object(setup, 'cli', side_effect=[{'openTaskId':'task-original'},
                {'openMessageId':'message-original', 'openConversationId':'different-group'}]):
            self.assertEqual(sender('正文')['status'], 'unknown')

    def test_concurrent_handle_cannot_recover_inflight_claim(self):
        self.add_event('a')
        entered, release = threading.Event(), threading.Event()
        def sender(body):
            entered.set()
            release.wait(3)
            return {'status':'sent'}
        first = start_delivery(self.svc, sender=sender)
        self.addCleanup(first.close)
        try:
            self.assertTrue(entered.wait(2))
            second = start_delivery(InventoryService(self.tmp.name), sender=sender)
            self.addCleanup(second.close)
            self.assertEqual(self.state('a')[0], 'sending')
            self.assertIsNone(second.thread)
        finally:
            release.set()
            first.close()
        self.assertEqual(self.state('a')[0], 'sent')

    def test_crashed_claim_is_unknown_before_new_background_work(self):
        self.add_event('a')
        with self.svc.db() as db:
            db.execute("UPDATE events SET status='sending'")
        calls = []
        handle = start_delivery(self.svc, sender=lambda body: calls.append(body) or {'status':'sent'})
        handle.close()
        self.assertEqual(self.state('a')[0], 'unknown')
        self.assertEqual(calls, [])

    def test_default_background_handle_never_constructs_live_sender(self):
        self.add_event('a')
        with patch('inventory_delivery.DwsSender', side_effect=AssertionError('live forbidden')):
            handle = start_delivery(self.svc)
            handle.close()
        self.assertEqual(self.state('a')[0], 'pending')

    def test_formatter_distinguishes_increase_and_source_age(self):
        event = self.add_event('a', 'stock_increase')
        self.svc.save_product(dict(sku='a', name='测试产品', category='内存', threshold=10))
        self.svc.record_message(dict(message_id='original', text='a 到货', created_at=1699990000, sender='经理', group='来源群'))
        body = format_events([event], self.svc, now=1700000000)
        self.assertIn('库存增加', body)
        self.assertIn('不等同于确认入库', body)
        self.assertNotIn('Asia/Shanghai', body)
        self.assertNotIn('original', body)
        self.assertNotIn('源消息 ID', body)
        self.assertIn('前', body)

    def test_authorized_message_callback_precedes_legacy_terms(self):
        events = [dict(messageId='m1', conversationId='g', senderId='s', text='产品型号到达'),
                  dict(messageId='m2', conversationId='g', senderId='other', text='产品型号到达')]
        received = []
        count = process_lines(map(json.dumps, events), webhook='fake', sources=(('g','s'),),
                              state_path=Path(self.tmp.name)/'state.json', terms=('价格',), on_event=received.append)
        self.assertEqual(count, 0)
        self.assertEqual([e['messageId'] for e in received], ['m1'])

    def test_callback_failure_keeps_legacy_forwarding(self):
        event = dict(messageId='m', conversationId='g', senderId='s', text='价格变化')
        with patch('suhao_cost_monitor.send_payload', return_value={'errcode':0}) as send:
            count = process_lines([json.dumps(event)], webhook='fake', sources=(('g','s'),),
                state_path=Path(self.tmp.name)/'state.json', on_event=lambda e: 1/0, terms=('价格',))
        self.assertEqual(count, 1)
        send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
