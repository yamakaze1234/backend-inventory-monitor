"""一键价格变化：模拟成本来源，验证小额变化、实际 BOM 和无通知副作用。"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from tests.test_profit_monitor import manifest, FakeSource
from profit_manifest import canonical, sha
from erp_price_monitor import ProfitMonitor
from inventory_price_changes import changes


class PriceChangesTests(unittest.TestCase):
    def setUp(self):
        output=Path(__file__).resolve().parents[1]/'outputs/price-changes-tests';output.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=output));self.path=self.root/'manifest.json';self.source=FakeSource()
        self.monitor=ProfitMonitor(self.root,source_provider=lambda:self.source)
        self.service=SimpleNamespace(root=self.root)

    def tearDown(self):self.monitor.stop()

    def bind(self,value):
        value['businessHash']=sha(value['configs']);value.pop('contentHash',None);value['contentHash']=sha(value)
        self.path.write_text(canonical(value),encoding='utf-8');self.monitor.bind(self.path)

    def test_one_yuan_change_and_scoped_config_quantities(self):
        value=manifest();value['configs'][0]['parts'][0]['qty']=2
        other=copy.deepcopy(value['configs'][0]);other['shopId']='other';other['parts'][0]['qty']=3;value['configs'].append(other)
        self.bind(value);self.monitor.check(allow_notifications=False)
        first=changes(self.service);self.assertEqual('baseline',first['parts'][0]['direction'])
        self.source.cost+=100;ident=self.monitor.check(allow_notifications=False)
        result=changes(self.service,dict(run_id=ident))
        self.assertEqual(100,result['parts'][0]['delta_cents']);self.assertEqual(1,result['changed_count'])
        self.assertEqual([200,300],sorted(r['cost_delta_cents'] for r in result['configs']))
        self.assertEqual(2,len(result['configs']))

    def test_view_does_not_send_queue_or_consume_reminder_anchor(self):
        self.bind(manifest());self.monitor.check()
        self.monitor.robots=Mock();self.monitor.robots.list_robots.return_value=[dict(id='one',enabled=True,channels=['profit'])]
        with self.monitor.db() as db:
            self.monitor._set(db,'notify',True);self.monitor._set(db,'next_check',123)
        self.source.cost+=10000;self.monitor.check(allow_notifications=False)
        self.monitor.robots.send.assert_not_called()
        with self.monitor.db() as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM events').fetchone()[0])
            self.assertEqual(10000,db.execute('SELECT anchor_cents FROM price_baselines WHERE active=1').fetchone()[0])
            self.assertEqual(123,self.monitor._get(db,'next_check'))
        self.assertEqual('up',changes(self.service)['parts'][0]['direction'])

    def test_missing_yesterday_unknown_prices_and_historical_qty(self):
        self.bind(manifest());self.monitor.check(allow_notifications=False)
        self.source.cost+=100;run=self.monitor.check(allow_notifications=False)
        day=changes(self.service,dict(comparison='day'))
        self.assertIsNone(day['parts'][0]['before_cents'])
        value=manifest(revision=2);value['configs'][0]['parts'][0]['qty']=9;self.bind(value)
        self.assertEqual(100,changes(self.service,dict(run_id=run))['configs'][0]['cost_delta_cents'])
        with self.monitor.db() as db:
            db.execute('UPDATE price_results SET valid=0,unit_cents=NULL WHERE run_id=?',(run,))
        self.assertEqual('unknown',changes(self.service,dict(run_id=run))['parts'][0]['direction'])

    def test_yesterday_uses_same_source_scope_only(self):
        self.bind(manifest());old=self.monitor.check(allow_notifications=False)
        self.source.cost+=123;new=self.monitor.check(allow_notifications=False)
        start=datetime.now(timezone(timedelta(hours=8))).replace(hour=0,minute=0,second=0,microsecond=0)
        with self.monitor.db() as db:db.execute('UPDATE price_results SET created=? WHERE run_id=?',((start-timedelta(hours=1)).timestamp(),old))
        result=changes(self.service,dict(run_id=new,comparison='day'))
        self.assertEqual(123,result['parts'][0]['delta_cents'])
        with self.monitor.db() as db:db.execute("UPDATE price_results SET sql_scope='different' WHERE run_id=?",(old,))
        self.assertIsNone(changes(self.service,dict(run_id=new,comparison='day'))['parts'][0]['before_cents'])


if __name__=='__main__':unittest.main()
