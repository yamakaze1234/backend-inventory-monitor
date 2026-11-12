"""发货工作台与简报边界，全部使用隔离数据。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import runpy
import unittest
from shipping_business import view
from inventory_business_actions import shipping_interval


class ShippingWorkbenchTests(unittest.TestCase):
    def test_page_and_period_do_not_change_matching_schedule(self):
        b=Mock()
        b.order_bot.configuration.return_value=dict(deployed=True,shippingReady=True,shipping=dict(bound=True,intervalMinutes=120))
        page=view(b,{})
        self.assertEqual(page['module'],'shipping')
        self.assertIn('interval',[f['action'] for f in page['sections'][0]['forms']])
        for bad in (True,0,14,1441,'a'):
            with self.assertRaises(ValueError):shipping_interval(b,dict(minutes=bad))
        b.order_bot.action.assert_not_called()
        shipping_interval(b,dict(minutes='60'))
        b.order_bot.action.assert_called_once_with('shipping-interval',dict(minutes=60))

    def test_large_report_stays_one_brief_without_order_details(self):
        path=Path(__file__).resolve().parents[1]/'integrations/order_backend/matching/shipping_monitor.py'
        with patch.dict('sys.modules',robot_config=SimpleNamespace(get_config=Mock(),validate=Mock())):
            module=runpy.run_path(str(path))
        report=dict(scanned=100,checkedAt='2026-11-12T18:00:00',errors=['敏感异常'],
            orders=[dict(shop='测试店',level='紧急',platformNo='private-order') for _ in range(100)],
            onlineSync=dict(enabled=True,complete=True,url='https://alidocs.dingtalk.com/test'))
        text=module['notification_summary'](report)
        self.assertLess(len(text),1500)
        self.assertNotIn('private-order',text)
        self.assertNotIn('敏感异常',text)
        self.assertIn('[查看发货在线明细]',text)
