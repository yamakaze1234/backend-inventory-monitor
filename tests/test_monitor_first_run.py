import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import source_monitor_supervisor as supervisor
from monitor_app_control import Controller, summarize_status, write_json


class FirstRunTests(unittest.TestCase):
    def test_inventory_only_start_does_not_launch_message_worker(self):
        from inventory_app import start_configured_message_worker
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller({'workspace': directory, 'portable': True})
            with patch.object(controller, 'ensure_current_worker') as launch:
                self.assertFalse(start_configured_message_worker(controller))
                launch.assert_not_called()
                write_json(controller.root / 'monitor_sources.json', [])
                self.assertFalse(start_configured_message_worker(controller))
                launch.assert_not_called()
                write_json(controller.root / 'monitor_sources.json', [{'GroupId': 'offline'}])
                self.assertTrue(start_configured_message_worker(controller))
                launch.assert_called_once_with()

    def test_missing_sources_waits_without_creating_config_and_reloads_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(supervisor, 'ROOT', root):
                self.assertEqual(supervisor.load_sources(), [])
                self.assertFalse((root / 'monitor_sources.json').exists())
                rows = [{'GroupId': 'offline-group', 'SenderId': 'offline-user'}]
                write_json(root / 'monitor_sources.json', rows)
                self.assertEqual(supervisor.load_sources(), rows)
                (root / 'monitor_sources.json').write_text('{broken', encoding='utf-8')
                with self.assertRaises(json.JSONDecodeError):
                    supervisor.load_sources()

    def test_unconfigured_worker_reports_setup_and_acknowledges_stop(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller({'workspace': directory, 'portable': True})
            snapshot = dict(heartbeat=time.time(), pid=123, enabled=False,
                            configured=False, sources=[])
            write_json(controller.runtime / 'status.json', snapshot)
            with patch('monitor_app_control.is_process_alive', return_value=True):
                self.assertEqual(controller.status()['state'], 'unconfigured')
                controller.stop()
                self.assertEqual(controller.status()['state'], 'paused')

    def test_first_worker_creates_nested_root_without_starting_listeners(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'new-user' / 'data'
            with patch.object(supervisor, 'ROOT', root), \
                 patch.object(supervisor, 'RUNTIME', root / 'monitor_runtime'), \
                 patch.object(supervisor, 'PORTABLE_MODE', True), \
                 patch.object(supervisor, 'client_running') as client, \
                 patch.object(supervisor.Worker, 'start') as start, \
                 patch.object(supervisor.time, 'sleep', side_effect=InterruptedError):
                with self.assertRaises(InterruptedError):
                    supervisor.main()
                client.assert_not_called()
                start.assert_not_called()
            status = json.loads((root / 'monitor_runtime' / 'status.json').read_text())
            self.assertFalse(status['configured'])
            self.assertFalse(status['enabled'])
            self.assertEqual(status['sources'], [])
