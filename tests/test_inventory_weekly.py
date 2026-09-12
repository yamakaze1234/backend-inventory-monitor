import unittest
from datetime import datetime
from unittest.mock import patch
from inventory_weekly import week_range, TZ, collect_range, summarize, timestamp

class WeeklyTests(unittest.TestCase):
    def test_report_contains_only_ai_product_summary(self):
        import tempfile
        from inventory_monitor import InventoryService
        from inventory_weekly import generate
        row=dict(text='原始聊天：5070预计到货20片',created_at=1789000000,sender='经理',group='来源群',message_id='m')
        response='{"products":[{"name":"5070","latest":"预计到货20片","changes":"","sources":["0"]}]}'
        with tempfile.TemporaryDirectory() as root:
            service=InventoryService(root)
            with patch('inventory_weekly.collect_range',return_value=([row],[{'SenderName':'经理','GroupName':'来源群'}])),patch('inventory_ai.call',return_value=response) as ai:
                path=generate(service,now=datetime(2026,9,11,15,tzinfo=TZ).timestamp())
            body=path.read_text('utf-8')
            ai.assert_called_once()
            self.assertIn('5070',body)
            self.assertIn('预计到货20片',body)
            for excluded in ('原始聊天','原文索引','来源明细','来源群','经理','[M1]','条消息'):
                self.assertNotIn(excluded,body)
    def test_custom_dates_include_end_day_and_cap_today(self):
        now=datetime(2026,9,11,15,tzinfo=TZ).timestamp()
        start,end=week_range(('2026-09-07','2026-09-09'),now)
        self.assertEqual(end,datetime(2026,9,10,tzinfo=TZ))
        self.assertEqual(start,datetime(2026,9,7,tzinfo=TZ))
        self.assertEqual(week_range(('2026-09-11','2026-09-11'),now)[1].hour,15)
        for dates in [('2026-09-10','2026-09-09'),('bad','2026-09-09'),('2026-09-11','2026-09-12')]:
            with self.assertRaises(ValueError): week_range(dates,now)
    def test_dws_time(self):
        expected=datetime(2026,9,11,14,5,56,tzinfo=TZ).timestamp()
        self.assertEqual(timestamp('2026-09-11 14:05:56'),expected)
        self.assertEqual(timestamp('2026-09-11T14:05:56+08:00'),expected)
        self.assertEqual(timestamp(expected*1000),expected)
    def test_week_bounds(self):
        now=datetime(2026,9,11,15,tzinfo=TZ)
        start,end=week_range('本周',now.timestamp())
        self.assertEqual(start,datetime(2026,9,7,tzinfo=TZ))
        self.assertEqual(end,now)
        start,end=week_range('上周',now.timestamp())
        self.assertEqual(start,datetime(2026,8,31,tzinfo=TZ))
        self.assertEqual(end,datetime(2026,9,7,tzinfo=TZ))

    def test_invalid_ai_reference_rejected(self):
        row=dict(text='5070 到货20片',created_at=1789000000,sender='甲',group='群',message_id='m')
        with patch('inventory_ai.call',return_value='{"products":[{"name":"5070","latest":"20片","changes":"","sources":["99"]}]}'):
            with self.assertRaises(ValueError): summarize(None,[row])

    def test_no_text_no_ai(self):
        with patch('inventory_ai.call') as call:
            self.assertIn('无可汇总',summarize(None,[]))
            call.assert_not_called()
