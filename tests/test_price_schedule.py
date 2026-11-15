"""固定时刻与工作台涨跌划分边界，全部使用隔离数据。"""
from datetime import datetime
from unittest.mock import patch
import unittest

from price_schedule import TZ, next_daily, normalize_times
from inventory_price_changes import movement
from tests.test_price_changes import PriceChangesTests
from tests.test_profit_monitor import manifest


class DailyPriceTests(unittest.TestCase):
    setUp = PriceChangesTests.setUp
    tearDown = PriceChangesTests.tearDown
    bind = PriceChangesTests.bind
    def test_schedule_persists_and_manual_check_keeps_time(self):
        self.bind(manifest())
        now = datetime(2026, 11, 8, 8, 30, tzinfo=TZ).timestamp()
        with patch('erp_price_monitor.time.time', return_value=now):
            self.monitor.set_schedule('09:00', True)
        expected = datetime(2026, 11, 8, 9, tzinfo=TZ).timestamp()
        self.assertEqual(expected, self.monitor.status()['nextCheck'])
        self.monitor.check(allow_notifications=False)
        self.assertEqual(expected, self.monitor.status()['nextCheck'])
        # 重启对象保留下一次执行点，不立即再查。
        from erp_price_monitor import ProfitMonitor
        restarted = ProfitMonitor(self.root)
        self.assertEqual(expected, restarted.status()['nextCheck'])
        self.assertEqual('daily', restarted.status()['scheduleMode'])

    def test_switch_modes_preserves_alternative_and_reschedules_only_on_save(self):
        self.bind(manifest())
        now = datetime(2026, 11, 8, 8, 30, tzinfo=TZ).timestamp()
        with patch('erp_price_monitor.time.time',return_value=now):
            self.monitor.set_schedule(['09:00','16:00'],True)
            self.monitor.set_schedule([],True,mode='interval',minutes='150')
            current=self.monitor.schedule_settings()
            self.assertEqual((current['scheduleMode'],current['nextCheck']),('interval',now+150*60))
            self.assertEqual(current['scheduleTimes'],['09:00','16:00'])
            with self.assertRaises(ValueError):
                self.monitor.set_schedule([],False,mode='interval',minutes='15.5')
            self.assertEqual(self.monitor.schedule_settings(),current)
            self.monitor.set_schedule(current['scheduleTimes'],True)
        current=self.monitor.schedule_settings()
        self.assertEqual((current['scheduleMode'],current['intervalSeconds']),('daily',150*60))
        self.assertEqual(current['nextCheck'],now+30*60)

    def test_busy_source_retries_three_times_without_consuming_day(self):
        self.bind(manifest());self.monitor.set_schedule('09:00',True)
        self.source.raise_error=True
        now=datetime(2026,11,8,9,tzinfo=TZ).timestamp()
        with patch('erp_price_monitor.time.time',return_value=now):
            for _ in range(3):
                self.monitor.check()
                self.assertEqual(now+300,self.monitor.status()['nextCheck'])
                self.assertEqual('failed',self.monitor.status()['lastRun']['status'])
            self.monitor.check()
            self.assertEqual(datetime(2026,11,9,9,tzinfo=TZ).timestamp(),self.monitor.status()['nextCheck'])
        self.source.raise_error=False
        self.monitor.check()
        self.assertEqual('',self.monitor.status()['lastError'])

    def test_sql_failed_batch_contract_retries_but_missing_price_does_not(self):
        self.bind(manifest());self.monitor.set_schedule('09:00',True)
        self.source.failed=['123']
        now=datetime(2026,11,8,9,tzinfo=TZ).timestamp()
        with patch('erp_price_monitor.time.time',return_value=now):
            self.monitor.check()
            self.assertEqual(now+300,self.monitor.status()['nextCheck'])
            self.assertEqual('failed',self.monitor.status()['lastRun']['status'])
            self.source.failed=[];self.source.cost=None
            self.monitor.check()
            self.assertEqual(datetime(2026,11,9,9,tzinfo=TZ).timestamp(),self.monitor.status()['nextCheck'])

    def test_invalid_schedule_is_atomic_and_completed_moves_to_tomorrow(self):
        self.bind(manifest())
        self.monitor.set_schedule('09:00', True)
        old = self.monitor.status()['nextCheck']
        with self.assertRaises(ValueError):self.monitor.set_schedule('25:00', False)
        self.assertEqual(old, self.monitor.status()['nextCheck'])
        now = datetime(2026, 11, 8, 9, 1, tzinfo=TZ).timestamp()
        with patch('erp_price_monitor.time.time', return_value=now):self.monitor.check()
        self.assertEqual(datetime(2026,11,9,9,tzinfo=TZ).timestamp(),self.monitor.status()['nextCheck'])


class PriceRulesTests(unittest.TestCase):
    def test_clock_boundaries(self):
        self.assertEqual(['09:00','18:00'],normalize_times('18:00，09:00,09:00'))
        now=datetime(2026,11,8,9,tzinfo=TZ).timestamp()
        self.assertEqual(datetime(2026,11,9,9,tzinfo=TZ).timestamp(),next_daily(now,['09:00']))
        for invalid in ('', '9:00', '24:00', '12:60'):
            with self.assertRaises(ValueError):normalize_times(invalid)

    def test_workbench_price_bands(self):
        self.assertEqual('up',movement(10000,10499)['band'])
        self.assertEqual('high',movement(10000,10500)['band'])
        self.assertEqual('down',movement(10000,9999)['band'])
        self.assertEqual(dict(band='high',percent=None),movement(0,1))
        self.assertEqual('same',movement(0,0)['band'])
        self.assertEqual('first',movement(None,100)['band'])
        self.assertEqual('missing',movement(100,None)['band'])
