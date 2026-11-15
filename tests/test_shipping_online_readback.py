"""核验在线表延迟可见时只重读，不重复新增或覆盖。"""
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class ShippingOnlineReadbackTests(unittest.TestCase):
    def test_create_waits_for_matching_cells_without_rewriting(self):
        path = Path(__file__).resolve().parents[1] / 'integrations/order_backend/matching/shipping_online.py'
        with patch.dict('sys.modules', shipping_monitor=SimpleNamespace(CN=None,date=None,identifier=None)):
            module = runpy.run_path(str(path))
        table = module['Table']({}, Path('.'))
        table.call = Mock(return_value={'data': {'newRecordIds': ['record-1']}})
        table.records = Mock(side_effect=[[], [{'cells': {'field': '旧值'}}], [{'cells': {'field': '新值'}}]])
        with patch('time.sleep'):
            self.assertEqual(table.write(None, {'field': '新值'}), 'record-1')
        self.assertEqual(table.call.call_count, 1)
        self.assertEqual(table.records.call_count, 3)

    def test_persistent_mismatch_stays_unconfirmed(self):
        path = Path(__file__).resolve().parents[1] / 'integrations/order_backend/matching/shipping_online.py'
        with patch.dict('sys.modules', shipping_monitor=SimpleNamespace(CN=None,date=None,identifier=None)):
            module = runpy.run_path(str(path))
        table = module['Table']({}, Path('.'))
        table.call = Mock(return_value={'data': {'newRecordIds': ['record-1']}})
        table.records = Mock(return_value=[])
        with patch('time.sleep'), self.assertRaisesRegex(ValueError, '回读不一致'):
            table.write(None, {'field': '新值'})
        self.assertEqual(table.call.call_count, 1)
        self.assertEqual(table.records.call_count, 6)
