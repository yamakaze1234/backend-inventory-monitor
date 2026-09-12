import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from inventory_monitor import InventoryService
from inventory_daily import TZ, save_settings, run_once
from inventory_ai import summarize


class EmptyDailyTests(unittest.TestCase):
    def test_empty_or_excluded_messages_do_not_call_ai(self):
        for rows in ([], [{'text': '  '}], [{'text': '成本表重新开表'}]):
            with self.subTest(rows=rows), patch('inventory_ai.call') as call:
                self.assertIsNone(summarize(None, rows, 0))
                call.assert_not_called()

    def test_no_clear_products_is_a_skip(self):
        with patch('inventory_ai.call', return_value='{"products": []}'):
            self.assertIsNone(summarize(None, [{'text': '普通交流', 'created_at': 0}], 0))

    def test_empty_slot_is_recorded_without_send_and_next_slot_still_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InventoryService(directory)
            save_settings(service, dict(enabled=True, times=['11:00', '16:00']))
            sender = Mock(return_value={'status': 'sent'})
            morning = datetime(2026, 9, 11, 11, tzinfo=TZ).timestamp()
            afternoon = datetime(2026, 9, 11, 16, tzinfo=TZ).timestamp()
            with patch('inventory_daily.render', return_value=None) as render:
                self.assertEqual(run_once(service, sender, morning)['status'], 'skipped')
                self.assertIsNone(run_once(service, sender, morning + 60))
                render.assert_called_once()
                sender.assert_not_called()
            with patch('inventory_daily.render', return_value='有效货源日报'):
                self.assertEqual(run_once(service, sender, afternoon)['status'], 'sent')
                sender.assert_called_once_with('有效货源日报')

    def test_collection_error_is_failure_not_empty_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InventoryService(directory)
            save_settings(service, dict(enabled=True, times=['11:00']))
            sender = Mock()
            with patch('inventory_daily.render', side_effect=RuntimeError('fixture')):
                result = run_once(service, sender, datetime(2026, 9, 11, 11, tzinfo=TZ).timestamp())
            self.assertEqual(result['status'], 'failed')
            sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
