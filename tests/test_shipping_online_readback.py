"""核验在线表延迟可见时只重读，不重复新增或覆盖。"""
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class ShippingOnlineReadbackTests(unittest.TestCase):
    def runtime(self):
        path=Path(__file__).resolve().parents[1]/'integrations/order_backend/matching/shipping_online.py'
        with patch.dict('sys.modules',shipping_monitor=SimpleNamespace(CN=None,date=None,identifier=None)):
            return runpy.run_path(str(path))

    def test_read_retries_empty_response_without_exposing_stderr(self):
        table=self.runtime()['Table'](dict(baseId='base',tableId='table',profile='fixture'),Path('.'))
        empty=SimpleNamespace(stdout='',stderr='private-token',returncode=1)
        good=SimpleNamespace(stdout='{"data":{}}',stderr='',returncode=0)
        with patch.dict('sys.modules',group_order_bot=SimpleNamespace(DWS='fixture')),patch('subprocess.run',side_effect=[empty,good]) as run,patch('time.sleep'):
            self.assertEqual(table.call(['+record-query']),{'data':{}})
            self.assertEqual(run.call_count,2)

    def test_unknown_write_is_not_repeated(self):
        module=self.runtime();table=module['Table'](dict(baseId='base',tableId='table',profile='fixture'),Path('.'))
        with patch.dict('sys.modules',group_order_bot=SimpleNamespace(DWS='fixture')),patch('subprocess.run',return_value=SimpleNamespace(stdout='',stderr='private-token',returncode=1)) as run:
            with self.assertRaises(module['UnconfirmedTableResponse']) as error:table.call(['record','update'],True)
            self.assertEqual(run.call_count,1)
            self.assertNotIn('private-token',str(error.exception))

    def test_lost_update_receipt_verified_by_read_only(self):
        module=self.runtime();table=module['Table']({},Path('.'))
        table.records=Mock(side_effect=[[{'cells':{'f':'old'}}],[{'cells':{'f':'new'}}]])
        table.call=Mock(side_effect=module['UnconfirmedTableResponse']('unknown'))
        self.assertEqual(table.write('known-record',{'f':'new'}),'known-record')
        self.assertEqual(table.call.call_count,1)

    def test_unknown_update_not_visible_keeps_failure(self):
        module=self.runtime();table=module['Table']({},Path('.'))
        table.records=Mock(return_value=[{'cells':{'f':'old'}}])
        table.call=Mock(side_effect=module['UnconfirmedTableResponse']('unknown'))
        with patch('time.sleep'),self.assertRaisesRegex(ValueError,'回读不一致'):table.write('known-record',{'f':'new'})
        self.assertEqual(table.call.call_count,1)

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
