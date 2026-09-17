import tempfile
import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from inventory_monitor import InventoryService
from inventory_daily import TZ, save_settings, run_once, render, ReportGenerationError
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


class SupplyContentTests(unittest.TestCase):
    rows = [dict(text='5070TI 库存告急，5080 重点推，5060TI 每周到货',
                 created_at=0, sender='来源', group='群')]

    def product(self, **values):
        return dict(dict(name='5070TI', spec='', supply='', arrival='', action='', sources=['0']), **values)

    def summarize_products(self, products):
        with patch('inventory_ai.call', return_value=json.dumps({'products': products})):
            return summarize(None, self.rows, 0)

    def test_promotion_does_not_block_supply_or_arrival_only_items(self):
        body = self.summarize_products([
            self.product(supply='库存告急'),
            self.product(name='5080', action='重点推'),
            self.product(name='5060TI 16G', arrival='每周稳定到货', action='不用惜售'),
        ])
        self.assertIn('库存告急', body)
        self.assertIn('5060TI 16G', body)
        self.assertIn('到货：每周稳定到货', body)
        self.assertNotIn('5080', body)
        self.assertNotIn('重点推', body)
        self.assertEqual(body.count('来源：'), 2)

    def test_all_promotion_or_empty_entries_skip(self):
        self.assertIsNone(self.summarize_products([self.product(action='重点推'), self.product()]))

    def test_invalid_schema_or_sources_still_fail(self):
        for invalid in [dict(sources=['9']), dict(sources=[]), dict(supply=None),
                        dict(name=''), dict(sources=[0]), dict(arrival='x' * 1601)]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.summarize_products([self.product(supply='库存告急'), self.product(**invalid)])


class GenerationRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = InventoryService(self.tmp.name)
        save_settings(self.service, dict(enabled=True, times=['11:00', '16:00']))
        self.now = datetime(2026, 9, 16, 11, tzinfo=TZ).timestamp()
        self.sender = Mock(return_value={'status': 'sent'})
        self.key = 'daily_supply:2026-09-16:11:00'

    def test_generation_retry_survives_restart_and_sends_once(self):
        with patch('inventory_daily.render', side_effect=[ReportGenerationError('ai'), '有效日报']) as rendering:
            first = run_once(self.service, self.sender, self.now)
            self.assertEqual(first['stage'], 'ai')
            self.assertTrue(first['retryable_generation'])
            self.sender.assert_not_called()
            self.assertIsNone(run_once(self.service, self.sender, self.now + 299))
            restarted = InventoryService(self.tmp.name)
            second = run_once(restarted, self.sender, self.now + 300)
            self.assertEqual(second['status'], 'sent')
            self.assertEqual(second['attempts'], 2)
            self.assertEqual(rendering.call_args.args[2], self.now)
            self.assertIsNone(run_once(restarted, self.sender, self.now + 1000))
            self.sender.assert_called_once_with('有效日报')

    def test_generation_retry_stops_after_three_attempts(self):
        with patch('inventory_daily.render', side_effect=RuntimeError('private error')) as rendering:
            for seconds in [0, 300, 900]:
                result = run_once(self.service, self.sender, self.now + seconds)
            self.assertFalse(result['retryable_generation'])
            self.assertEqual(result['attempts'], 3)
            self.assertNotIn('private', json.dumps(result))
            self.assertIsNone(run_once(self.service, self.sender, self.now + 1200))
            self.assertEqual(rendering.call_count, 3)
            self.sender.assert_not_called()

    def test_delivery_failure_and_ambiguity_are_never_retried(self):
        for outcome in [dict(status='failed'), dict(status='unknown'), dict(status='partial'), TimeoutError()]:
            with self.subTest(outcome=outcome):
                with self.service.db() as db:
                    db.execute('DELETE FROM meta WHERE key=?', (self.key,))
                sender = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
                with patch('inventory_daily.render', return_value='有效日报'):
                    result = run_once(self.service, sender, self.now)
                    self.assertFalse(result['retryable_generation'])
                    self.assertIsNone(run_once(self.service, sender, self.now + 900))
                    sender.assert_called_once()

    def test_legacy_and_inflight_records_are_never_replayed(self):
        for status in ['failed', 'sending', 'sent', 'skipped', 'unknown', 'partial']:
            with self.subTest(status=status), self.service.db() as db:
                self.service.put(db, self.key, dict(status=status, at=self.now))
            with patch('inventory_daily.render') as rendering:
                self.assertIsNone(run_once(self.service, self.sender, self.now + 900))
                rendering.assert_not_called()

    def test_new_slot_supersedes_old_retry(self):
        with patch('inventory_daily.render', side_effect=ReportGenerationError('collection')):
            run_once(self.service, self.sender, self.now)
        with patch('inventory_daily.render', return_value='下午日报') as rendering:
            result = run_once(self.service, self.sender, self.now + 5 * 3600)
            self.assertEqual(result['attempts'], 1)
            self.assertEqual(rendering.call_args.args[2], self.now + 5 * 3600)
            self.sender.assert_called_once_with('下午日报')

    def test_collection_diagnostic_never_exposes_exception_text(self):
        with patch('inventory_history.collect', side_effect=ValueError('private error')):
            result = run_once(self.service, self.sender, self.now)
        self.assertEqual(result['stage'], 'collection')
        self.assertEqual(result['reason'], '货源历史采集失败，未发送。')
        self.sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
