from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from inventory_connection_alert import run_once
from inventory_monitor import InventoryService


class ConnectionAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = InventoryService(self.temp.name)
        self.sender = Mock()
        self.sender.send.return_value = {'status': 'sent'}

    def healthy(self, now):
        self.service.connection_update('', now=now)
        run_once(self.service, self.sender, now=now)

    def test_initial_wait_and_short_interruption_do_not_send(self):
        self.assertFalse(run_once(self.service, self.sender, now=100))
        self.healthy(100)
        self.assertFalse(run_once(self.service, self.sender, now=169))
        self.sender.send.assert_not_called()

    def test_outage_sends_once_across_restart_and_rearms_after_recovery(self):
        self.healthy(100)
        self.assertFalse(run_once(self.service, self.sender, now=170))
        self.assertTrue(run_once(self.service, self.sender, now=470))
        restarted = InventoryService(self.temp.name)
        self.assertFalse(run_once(restarted, self.sender, now=500))
        self.sender.send.assert_called_once()
        channel, job, text, occurred = self.sender.send.call_args.args
        self.assertEqual(channel, 'inventory')
        self.assertIn('ERP 连接断开', text)
        self.assertIn('最近成功采集', text)
        self.healthy(501)
        self.assertFalse(run_once(restarted, self.sender, now=571))
        self.assertTrue(run_once(restarted, self.sender, now=871))
        self.assertEqual(self.sender.send.call_count, 2)
        self.assertNotEqual(job, self.sender.send.call_args.args[1])

    def test_collector_error_alerts_even_with_fresh_heartbeat_and_no_raw_error(self):
        self.healthy(100)
        self.service.connection_update('sensitive raw error', now=101)
        self.assertFalse(run_once(self.service, self.sender, now=101))
        self.assertTrue(run_once(self.service, self.sender, now=401))
        self.assertNotIn('sensitive raw error', self.sender.send.call_args.args[2])
        self.service.connection_update(now=120)
        self.assertFalse(run_once(self.service, self.sender, now=120))

    def test_pause_suppresses_alert(self):
        self.healthy(100)
        self.service.set_enabled(False)
        self.assertFalse(run_once(self.service, self.sender, now=200))
        self.sender.send.assert_not_called()

    def test_recovers_during_retry_window_without_alert(self):
        self.healthy(100)
        self.assertFalse(run_once(self.service, self.sender, now=170))
        self.healthy(200)
        self.assertFalse(run_once(self.service, self.sender, now=269))
        self.sender.send.assert_not_called()

    def test_explicit_recovery_failure_alerts_without_waiting_full_timeout(self):
        self.healthy(100)
        self.assertFalse(run_once(self.service, self.sender, now=170))
        with self.service.db() as db:
            self.service.put(db, 'erp_recovery', {'state': 'captcha', 'at': 180})
        self.assertTrue(run_once(self.service, self.sender, now=180))
        self.assertFalse(run_once(self.service, self.sender, now=181))
        self.sender.send.assert_called_once()

    def test_ambiguous_send_is_not_repeated(self):
        self.healthy(100)
        self.sender.send.side_effect = TimeoutError
        self.assertFalse(run_once(self.service, self.sender, now=200))
        self.assertTrue(run_once(self.service, self.sender, now=500))
        self.assertFalse(run_once(self.service, self.sender, now=300))
        self.sender.send.assert_called_once()

    def test_skipped_and_failed_send_are_not_repeated(self):
        for status in ('skipped', 'failed', 'partial'):
            with self.subTest(status=status):
                self.healthy(100)
                self.sender.send.return_value = {'status': status}
                before = self.sender.send.call_count
                self.assertFalse(run_once(self.service, self.sender, now=200))
                self.assertTrue(run_once(self.service, self.sender, now=500))
                self.assertFalse(run_once(self.service, self.sender, now=300))
                self.assertEqual(self.sender.send.call_count, before + 1)
