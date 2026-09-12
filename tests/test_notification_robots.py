import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from notification_robots import RobotStore, store_for_service, forward_source
from inventory_monitor import InventoryService
from inventory_delivery import RobotSender, DeliveryWorker
from suhao_cost_monitor import process_lines


class RobotsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RobotStore(self.root)
        self.now = time.time() + 2

    def add(self, name='部门一', channels=('inventory', 'daily', 'messages')):
        # Only synthetic credentials; all network transports are injected.
        with patch('monitor_secret_store.protect', side_effect=lambda b: b'encrypted-' + b):
            return self.store.save(name, 'https://oapi.dingtalk.com/robot/send?access_token=synthetic-' + name, channels)

    def send(self, channel, key, transport, occurred=None):
        return self.store.send(channel, key, {'synthetic': True}, self.now if occurred is None else occurred, transport)

    def test_all_three_channels_fan_out_with_independent_scopes(self):
        self.add()
        self.add('部门二', ['inventory'])
        for channel, count in [('inventory', 2), ('daily', 1), ('messages', 1)]:
            sender = Mock(return_value={'status': 'sent'})
            result = self.send(channel, channel, sender)
            self.assertEqual(result['status'], 'sent')
            self.assertEqual(sender.call_count, count)
            self.send(channel, channel, sender)
            self.assertEqual(sender.call_count, count)

    def test_pause_resume_drops_backlog_and_keeps_other_robot(self):
        one = self.add()
        self.add('部门二')
        with patch('notification_robots.time.time', return_value=self.now):
            self.store.set_enabled(one, False)
        transport = Mock(return_value={'status': 'sent'})
        self.assertEqual(self.send('messages', 'paused', transport)['status'], 'sent')
        self.assertEqual(transport.call_count, 1)
        with patch('notification_robots.time.time', return_value=self.now + 10):
            self.store.set_enabled(one, True)
        self.send('messages', 'paused', transport, self.now + 11)
        self.assertEqual(transport.call_count, 1)
        old = self.send('messages', 'arrives-late', transport, self.now + 5)
        self.assertEqual([r['status'] for r in old['recipients']], ['skipped', 'sent'])
        fresh = self.send('messages', 'fresh', transport, self.now + 12)
        self.assertEqual([r['status'] for r in fresh['recipients']], ['sent', 'sent'])

    def test_failed_recipient_retry_does_not_repeat_success(self):
        self.add()
        self.add('部门二')
        transport = Mock(side_effect=[{'status': 'sent'}, {'status': 'failed', 'retryable': True}, {'status': 'sent'}])
        result = self.send('inventory', 'event', transport)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['retryable'])
        self.assertEqual(self.send('inventory', 'event', transport)['status'], 'sent')
        self.assertEqual(transport.call_count, 3)

    def test_exception_and_unknown_do_not_block_or_retry(self):
        self.add()
        self.add('部门二')
        transport = Mock(side_effect=[TimeoutError('synthetic'), {'status': 'sent'}])
        result = self.send('daily', 'slot', transport)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['retryable'])
        self.send('daily', 'slot', transport)
        self.assertEqual(transport.call_count, 2)

    def test_pause_before_second_send_prevents_delivery(self):
        self.add()
        second = self.add('部门二')
        def transport(*args):
            self.store.set_enabled(second, False)
            return {'status': 'sent'}
        result = self.send('inventory', 'event', transport)
        self.assertEqual([r['status'] for r in result['recipients']], ['sent', 'skipped'])

    def test_new_robots_and_new_channels_do_not_get_old_jobs(self):
        one = self.add(channels=['inventory'])
        sender = Mock(return_value={'status': 'sent'})
        self.send('daily', 'slot', sender)
        self.add('部门二')
        self.store.save('部门一', channels=['inventory', 'daily'], robot_id=one)
        self.assertEqual(self.send('daily', 'slot', sender)['status'], 'skipped')
        sender.assert_not_called()

    def test_concurrent_sender_claims_once(self):
        self.add()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def transport(*args):
            calls.append(1)
            entered.set()
            release.wait(3)
            return {'status': 'sent'}
        worker = threading.Thread(target=lambda: self.send('messages', 'one', transport))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.send('messages', 'one', transport)
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(len(calls), 1)

    def test_legacy_migration_once_with_original_channel_scopes(self):
        inventory = InventoryService(self.root / 'inventory')
        (inventory.root / 'inventory_robot.dat').write_bytes(b'ciphertext-inventory')
        (self.root / 'delivery.dat').write_bytes(b'ciphertext-messages')
        with inventory.db() as db:
            inventory.put(db, 'daily_source_root', str(self.root))
        store = store_for_service(inventory)
        robots = {r['id']: r for r in store.list_robots()}
        self.assertEqual(robots['legacy-inventory']['channels'], ['inventory', 'daily'])
        self.assertEqual(robots['legacy-messages']['channels'], ['messages'])
        store.delete('legacy-messages')
        self.assertEqual(len(store_for_service(inventory).list_robots()), 1)
        self.assertNotIn('secret', json.dumps(robots))

    def test_edit_without_webhook_and_channel_resume_epoch(self):
        ident = self.add()
        with patch('notification_robots.time.time', return_value=self.now + 10):
            self.store.save('新备注', channels=['daily'], robot_id=ident)
        sender = Mock(return_value={'status': 'sent'})
        self.assertEqual(self.send('daily', 'kept', sender)['status'], 'sent')
        with patch('notification_robots.time.time', return_value=self.now + 20):
            self.store.save('新备注', channels=['daily', 'inventory'], robot_id=ident)
        self.assertEqual(self.send('inventory', 'old', sender)['status'], 'skipped')

    def test_instant_forward_uses_router_and_journals_while_paused(self):
        ident = self.add()
        self.store.set_enabled(ident, False)
        event = dict(messageId='m', conversationId='g', senderId='s', text='价格变化', createTime=self.now)
        journal = Mock()
        with patch('notification_robots.send_encrypted', return_value={'status': 'sent'}) as send:
            callback = lambda e, p: forward_source(self.root, {}, e, p)
            process_lines([json.dumps(event)], webhook='', sources=(('g', 's'),), state_path=self.root / 'seen.json',
                          terms=('价格',), sender=callback, on_event=journal)
            self.store.set_enabled(ident, True)
            process_lines([json.dumps(event)], webhook='', sources=(('g', 's'),), state_path=self.root / 'seen.json',
                          terms=('价格',), sender=callback, on_event=journal)
            send.assert_not_called()
        self.assertEqual(journal.call_count, 2)

    def test_duplicate_actual_robot_rejected_for_overlapping_channels(self):
        self.add('部门一', ['inventory'])
        with self.assertRaises(ValueError):
            self.add('部门一', ['inventory', 'daily'])
        self.add('部门一', ['daily'])

    def test_daily_runtime_fans_out_and_does_not_repeat_slot(self):
        from inventory_daily import run_once, save_settings, TZ
        from datetime import datetime
        service = InventoryService(self.root)
        stamp = datetime(2026, 9, 11, 11, 1, tzinfo=TZ).timestamp()
        with patch('notification_robots.time.time', return_value=stamp - 120):
            self.add()
            self.add('部门二')
        save_settings(service, {'enabled': True, 'times': ['11:00', '16:00']})
        with patch('inventory_daily.render', return_value='货源情况 测试'), patch(
                'notification_robots.send_encrypted', side_effect=[{'status': 'failed'}, {'status': 'sent'}]) as transport:
            result = run_once(service, RobotSender(service), stamp)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(len(result['recipients']), 2)
            self.assertIsNone(run_once(service, RobotSender(service), stamp + 30))
            self.assertEqual(transport.call_count, 2)

    def test_inventory_retry_keeps_job_when_one_product_is_cancelled(self):
        service = InventoryService(self.root)
        self.add()
        self.add('部门二')
        for ident in ('a', 'b'):
            event = dict(id=ident, sku=ident, name='测试产品', kind='low', threshold=10,
                         observed_at=self.now, check_id='check', after={'able': 2})
            with service.db() as db:
                db.execute('INSERT INTO events(id,data,created) VALUES (?,?,?)', (ident, json.dumps(event), self.now))
        worker = DeliveryWorker(service, RobotSender(service))
        with patch('notification_robots.send_encrypted', side_effect=[{'status': 'sent'},
                {'status': 'failed', 'retryable': True}, {'status': 'sent'}]) as transport:
            self.assertEqual(worker.run_once(self.now), 2)
            with service.db() as db:
                db.execute("UPDATE events SET status='cancelled' WHERE id='a'")
            self.assertEqual(worker.run_once(self.now + 60), 1)
            self.assertEqual(transport.call_count, 3)
            self.assertEqual(service.status()['events'][0]['delivery_status'], 'sent')


if __name__ == '__main__':
    unittest.main()
