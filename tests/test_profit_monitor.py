"""Offline acceptance for ERP unit-price monitoring; never connects or sends."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from profit_manifest import canonical, read_manifest, sha
from profit_monitor import CoordinatedProfitSource, ProfitMonitor
from erp_price_monitor import PRICE_BASIS, PRICE_SOURCE_SCOPE


def manifest(revision=1, *, price=100000, tax=10000, goods_id='123'):
    parts = [dict(lineId='cpu', slot='CPU', name='处理器', goodsId=goods_id, qty=1,
                  taxCents=tax, taxUpdatedAt='2026-09-24T00:00:00Z')]
    configs = [dict(shopId='intel', configId='c1', productId='p1', product='链接', name='配置1',
                    priceCents=price, installment=0, feeCents=0, revision='r1', parts=parts)]
    value = dict(contract='profit-monitor/v1', schemaVersion=1, complete=True, sourceId='device-1',
                 workspaceId='workspace-1', revision=revision, publishedAt='2026-09-24T00:00:00Z',
                 formulaVersion='profit/v1', businessHash=sha(configs), configs=configs)
    value['contentHash'] = sha(value)
    return value


def rehash(value):
    value['businessHash'] = sha(value['configs'])
    value['contentHash'] = sha({k: v for k, v in value.items() if k != 'contentHash'})
    return value


class FakeSource:
    def __init__(self):
        self.cost = 10000
        self.scope = 'sql:fixture-A'
        self.failed = []
        self.calls = []
        self.raise_error = False

    def read_profit_costs(self, ids):
        self.calls.append(set(ids))
        if self.raise_error:
            raise ValueError('synthetic source outage')
        return dict(costs={gid: self.cost for gid in ids if gid not in self.failed and self.cost is not None},
                    failed=list(self.failed), sourceScope=self.scope, collectedAt=1234567890.0)


class PriceMonitorTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.verification'
        base.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='erp-price-test-', dir=base))
        self.file = self.folder / 'manifest.json'
        self.source = FakeSource()
        self.monitor = ProfitMonitor(self.folder, source_provider=lambda: self.source)

    def tearDown(self):
        self.monitor.stop()
        # The isolated directory is recycled after the suite; never hard-delete it.

    def write(self, value):
        self.file.write_text(canonical(value), encoding='utf-8')

    def events(self):
        with self.monitor.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events WHERE kind='price-v2' ORDER BY created,rowid")]

    def results(self, run):
        with self.monitor.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM price_results WHERE run_id=? ORDER BY goods_id', (run,))]

    def test_first_price_and_exact_hundred_yuan_both_directions(self):
        self.write(manifest(price=10000, tax=60000))  # Former profit mode would alert; price mode must not.
        self.monitor.bind(self.file)
        first = self.monitor.check()
        self.assertEqual(self.results(first)[0]['unit_cents'], 10000)
        self.assertFalse(self.results(first)[0]['changed'])
        self.assertEqual(self.events(), [])
        self.assertFalse(self.monitor.status()['notify'])
        self.source.cost = 19999
        second = self.monitor.check()
        self.assertEqual(self.results(second)[0]['previous_cents'], 10000)
        self.assertEqual(self.results(second)[0]['anchor_cents'], 10000)
        self.assertEqual(self.results(second)[0]['anchor_delta_cents'], 9999)
        self.assertEqual(self.results(second)[0]['changed'], 0)
        self.assertEqual(self.events(), [])
        self.source.cost = 20000
        self.monitor.check()
        self.source.cost = 10001
        almost_down = self.monitor.check()
        self.assertEqual(self.results(almost_down)[0]['anchor_delta_cents'], -9999)
        self.assertEqual(len(self.events()), 1)
        self.source.cost = 10000
        self.monitor.check()
        self.assertEqual(len(self.events()), 2)
        text = '\n'.join(json.loads(event['payload'])['text']['content'] for event in self.events())
        for word in ('goods_id 123', '累计上涨 100.00', '累计下降 100.00', '受影响配置', '配置1', '公司大库库存成本'):
            self.assertIn(word, text)
        self.assertNotIn('利润', text)
        self.assertNotIn('核算', text)
        self.assertNotIn('每日', text)
        self.monitor.check()
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(len(set(event['id'] for event in self.events())), 2)
        self.source.cost = 20000
        self.monitor.check()
        self.assertEqual(len(self.events()), 3)

    def test_small_steps_accumulate_until_anchor_moves(self):
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.check()
        for cost, delta in ((13000, 3000), (16000, 6000), (19999, 9999)):
            self.source.cost = cost
            run = self.monitor.check()
            row = self.results(run)[0]
            self.assertEqual(row['anchor_cents'], 10000)
            self.assertEqual(row['anchor_delta_cents'], delta)
            self.assertEqual(row['changed'], 0)
            self.assertEqual(self.events(), [])
        with self.monitor.db() as db:
            latest, anchor = db.execute('SELECT unit_cents,anchor_cents FROM price_baselines').fetchone()
        self.assertEqual((latest, anchor), (19999, 10000))
        self.source.cost = 20000
        triggered = self.monitor.check()
        self.assertEqual(self.results(triggered)[0]['changed'], 1)
        with self.monitor.db() as db:
            latest, anchor = db.execute('SELECT unit_cents,anchor_cents FROM price_baselines').fetchone()
        self.assertEqual((latest, anchor), (20000, 20000))
        self.source.cost = 29999
        self.monitor.check()
        self.assertEqual(len(self.events()), 1)
        self.source.cost = 30000
        self.monitor.check()
        self.assertEqual(len(self.events()), 2)

    def test_event_insert_failure_rolls_back_anchor_and_latest_together(self):
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.check()
        self.source.cost = 20000
        with self.monitor.db() as db:
            db.execute('''CREATE TRIGGER fail_price_event BEFORE INSERT ON events
                WHEN NEW.kind='price-v2' BEGIN SELECT RAISE(ABORT,'synthetic event failure'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic event failure'):
            self.monitor.check()
        with self.monitor.db() as db:
            self.assertEqual(tuple(db.execute('SELECT unit_cents,anchor_cents FROM price_baselines').fetchone()),
                             (10000, 10000))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM price_results').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM runs ORDER BY started DESC LIMIT 1").fetchone()[0], 'failed')
            db.execute('DROP TRIGGER fail_price_event')
        self.monitor.check()
        self.assertEqual(len(self.events()), 1)

    def test_business_fields_and_duplicate_references_do_not_alert(self):
        value = manifest()
        extra = copy.deepcopy(value['configs'][0])
        extra.update(configId='c2', name='配置2', shopId='amd')
        value['configs'].append(extra)
        self.write(rehash(value))
        self.monitor.bind(self.file)
        self.monitor.check()
        self.assertEqual(self.source.calls, [{'123'}])
        changed = copy.deepcopy(value)
        changed['revision'] = 2
        for config in changed['configs']:
            config.update(priceCents=1, installment=24, feeCents=10)
            config['parts'][0].update(qty=2, taxCents=99999)
        self.write(rehash(changed))
        self.monitor.check()
        self.assertEqual(self.events(), [])
        self.source.cost = 20000
        self.monitor.check()
        self.assertEqual(self.source.calls, [{'123'}, {'123'}, {'123'}])
        self.assertEqual(len(self.events()), 1)
        text = json.loads(self.events()[0]['payload'])['text']['content']
        self.assertIn('配置1', text)
        self.assertIn('配置2', text)
        self.assertIn('受影响配置：2 套', text)

    def test_failed_missing_and_zero_cost_preserve_baseline(self):
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.check()
        self.source.failed = ['123']
        self.source.cost = 0
        failed = self.monitor.check()
        self.assertFalse(self.results(failed)[0]['valid'])
        self.assertEqual(self.events(), [])
        self.source.failed = []
        self.source.cost = None
        missing = self.monitor.check()
        self.assertFalse(self.results(missing)[0]['valid'])
        self.source.raise_error = True
        outage = self.monitor.check()
        self.assertFalse(self.results(outage)[0]['valid'])
        self.source.raise_error = False
        self.source.cost = 0
        recovered = self.monitor.check()
        result = self.results(recovered)[0]
        self.assertTrue(result['valid'])
        self.assertEqual(result['previous_cents'], 10000)
        self.assertEqual(result['unit_cents'], 0)
        self.assertEqual(result['changed'], 1)
        self.assertEqual(len(self.events()), 1)
        self.source.cost = 1
        self.monitor.check()
        self.assertEqual(len(self.events()), 1)
        self.source.cost = 10000
        self.monitor.check()
        self.assertEqual(len(self.events()), 2)

    def test_sql_scope_and_workspace_changes_start_new_baseline(self):
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.check()
        self.source.scope = 'sql:fixture-B'
        self.source.cost = 20000
        run = self.monitor.check()
        self.assertIsNone(self.results(run)[0]['previous_cents'])
        self.assertEqual(self.events(), [])
        self.source.scope = 'sql:fixture-A'
        self.source.cost = 30000
        run = self.monitor.check()
        self.assertIsNone(self.results(run)[0]['previous_cents'])
        self.assertEqual(self.events(), [])
        changed = manifest(2)
        changed['workspaceId'] = 'workspace-2'
        self.monitor.unbind()
        self.write(rehash(changed))
        self.monitor.bind(self.file)
        run = self.monitor.check()
        self.assertIsNone(self.results(run)[0]['previous_cents'])

    def test_removing_last_reference_ends_active_baseline(self):
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.check()
        empty = manifest(2)
        empty['configs'] = []
        self.write(rehash(empty))
        self.monitor.check()
        with self.monitor.db() as db:
            self.assertEqual(db.execute('SELECT active FROM price_baselines').fetchone()[0], 0)
        self.write(manifest(3))
        self.source.cost = 11000
        run = self.monitor.check()
        self.assertIsNone(self.results(run)[0]['previous_cents'])
        self.assertEqual(self.events(), [])

    def test_deleted_and_readded_between_checks_starts_new_baseline(self):
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.check()
        empty = manifest(2)
        empty['configs'] = []
        self.write(rehash(empty))
        self.monitor.bind(self.file)
        with self.monitor.db() as db:
            self.assertEqual(db.execute('SELECT active FROM price_baselines').fetchone()[0], 0)
        self.write(manifest(3))
        self.monitor.bind(self.file)
        self.source.cost = 11000
        run = self.monitor.check()
        self.assertIsNone(self.results(run)[0]['previous_cents'])
        self.assertEqual(self.events(), [])

    def test_same_config_multiple_slots_counts_one_affected_config(self):
        value = manifest()
        second = copy.deepcopy(value['configs'][0]['parts'][0])
        second.update(lineId='fan', slot='风扇', name='配件同ERP商品')
        value['configs'][0]['parts'].append(second)
        self.write(rehash(value))
        self.monitor.bind(self.file)
        self.monitor.check()
        self.source.cost += 10000
        run = self.monitor.check()
        self.assertEqual(len(self.source.calls[-1]), 1)
        self.assertEqual(self.results(run)[0]['changed'], 1)
        self.assertEqual(self.monitor.status()['lastResults'][0]['affectedConfigs'], 1)
        self.assertIn('受影响配置：1 套', json.loads(self.events()[0]['payload'])['text']['content'])

    def test_external_bind_during_query_cannot_resurrect_deleted_baseline(self):
        import threading
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.check()
        entered, release = threading.Event(), threading.Event()
        original = self.source.read_profit_costs
        def blocked(ids):
            entered.set()
            release.wait(5)
            return original(ids)
        self.source.read_profit_costs = blocked
        worker = threading.Thread(target=self.monitor.check)
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            empty = manifest(2)
            empty['configs'] = []
            self.write(rehash(empty))
            with self.assertRaisesRegex(ValueError, '检测正在运行'):
                self.monitor.bind(self.file)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.monitor.bind(self.file)
        self.write(manifest(3))
        self.monitor.bind(self.file)
        self.source.cost = 11000
        run = self.monitor.check()
        self.assertIsNone(self.results(run)[0]['previous_cents'])
        self.assertEqual(self.events(), [])

    def test_partial_delivery_retries_only_existing_recipients(self):
        class Robots:
            def __init__(self):
                self.calls = []
            def list_robots(self):
                return [dict(id='r1', name='一', enabled=True, channels=['profit']),
                        dict(id='r2', name='二', enabled=True, channels=['profit'])]
            def send(self, channel, job, payload, occurred_at, recipient_ids=None):
                self.calls.append((channel, job, recipient_ids))
                return dict(status='partial' if len(self.calls) == 1 else 'sent')
        robots = Robots()
        self.monitor.robots = robots
        self.write(manifest())
        self.monitor.bind(self.file)
        self.monitor.set_notifications(True)
        self.monitor.check()
        self.assertEqual(robots.calls, [])
        self.source.cost += 10000
        self.monitor.check()
        self.assertEqual(len(robots.calls), 1)
        self.assertEqual(robots.calls[0][0], 'profit')  # Stable existing channel key.
        self.assertEqual(robots.calls[0][2], ['r1', 'r2'])
        self.assertEqual(self.events()[0]['status'], 'partial')
        with self.monitor.db() as db:
            self.assertEqual(db.execute('SELECT anchor_cents FROM price_baselines').fetchone()[0], 20000)
        self.monitor.deliver_pending()
        self.assertEqual(self.events()[0]['status'], 'sent')
        self.assertEqual(robots.calls[0][1:], robots.calls[1][1:])
        self.source.cost = 29999
        self.monitor.check()
        self.assertEqual(len(self.events()), 1)

    def test_manifest_validation_and_last_good_snapshot(self):
        self.write(manifest())
        self.monitor.bind(self.file)
        bad = manifest(2)
        bad['configs'][0]['priceCents'] = 1
        self.write(bad)
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.monitor.refresh()
        self.assertEqual(self.monitor.status()['revision'], 1)
        other = manifest(2)
        other['sourceId'] = 'other'
        self.write(rehash(other))
        with self.assertRaisesRegex(ValueError, '来源'):
            self.monitor.refresh()
        self.assertEqual(self.monitor.status()['sourceId'], 'device-1')
        self.write(manifest(0))
        with self.assertRaisesRegex(ValueError, '回退'):
            self.monitor.refresh()
        duplicate = manifest(2)
        duplicate['configs'].append(copy.deepcopy(duplicate['configs'][0]))
        self.write(rehash(duplicate))
        with self.assertRaisesRegex(ValueError, '重复'):
            read_manifest(self.file)

    def test_legacy_upgrade_preserves_history_and_cancels_old_backlog(self):
        self.monitor.stop()
        with closing(sqlite3.connect(self.monitor.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('enabled','true')")
            db.execute("INSERT OR REPLACE INTO settings VALUES('notify','true')")
            db.execute("DELETE FROM settings WHERE key='monitor_mode'")
            db.execute("INSERT INTO runs(id,source_id,revision,started,status,kind) VALUES('old-run','old',1,1,'completed','profit')")
            db.execute("INSERT INTO events(id,shop_id,created,payload,status,recipients,kind) VALUES('old-event','s',1,'{}','pending','[\"r1\"]','profit')")
            db.execute('CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, details TEXT)')
            db.execute("INSERT INTO results VALUES('old-result','historical')")
        self.monitor = ProfitMonitor(self.folder, source_provider=lambda: self.source)
        status = self.monitor.status()
        self.assertFalse(status['enabled'])
        self.assertFalse(status['notify'])
        self.assertEqual(status['events'], [])
        self.assertIsNone(status['lastRun'])
        with self.monitor.db() as db:
            self.assertEqual(db.execute("SELECT status FROM events WHERE id='old-event'").fetchone()[0], 'cancelled')
            self.assertEqual(db.execute("SELECT details FROM results WHERE id='old-result'").fetchone()[0], 'historical')
        self.monitor.deliver_pending()

    def test_v1_price_upgrade_anchors_at_last_valid_observation(self):
        old_folder = self.folder / 'old-price-v1'
        old_folder.mkdir()
        old_path = old_folder / 'profit_monitor.sqlite3'
        old_snapshot = manifest()
        sql_scope = sha([PRICE_SOURCE_SCOPE, self.source.scope, *PRICE_BASIS])
        with closing(sqlite3.connect(old_path)) as db, db:
            db.executescript('''
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE snapshot(id INTEGER PRIMARY KEY,content TEXT,received REAL);
                CREATE TABLE runs(id TEXT PRIMARY KEY,source_id TEXT,revision INTEGER,started REAL,finished REAL,status TEXT,error TEXT,kind TEXT);
                CREATE TABLE events(id TEXT PRIMARY KEY,shop_id TEXT,created REAL,payload TEXT,status TEXT,recipients TEXT,kind TEXT);
                CREATE TABLE price_baselines(source_id TEXT,workspace_id TEXT,sql_scope TEXT,goods_id TEXT,
                    unit_cents INTEGER,last_result_id TEXT,active INTEGER,
                    PRIMARY KEY(source_id,workspace_id,sql_scope,goods_id));
                CREATE TABLE price_results(id TEXT PRIMARY KEY,run_id TEXT,source_id TEXT,workspace_id TEXT,
                    sql_scope TEXT,goods_id TEXT,valid INTEGER,unit_cents INTEGER,previous_cents INTEGER,
                    changed INTEGER,reason TEXT,details TEXT,created REAL,UNIQUE(run_id,goods_id));
            ''')
            for key, value in [('monitor_mode', 'erp-unit-price/v1'), ('enabled', True),
                               ('notify', True), ('price_sql_scope', sql_scope)]:
                db.execute('INSERT INTO settings VALUES(?,?)', (key, canonical(value)))
            db.execute('INSERT INTO snapshot VALUES(1,?,1)', (canonical(old_snapshot),))
            db.execute("INSERT INTO runs VALUES('v1-run','device-1',1,1,2,'completed',NULL,'price')")
            db.execute("INSERT INTO events VALUES('v1-event','ERP单价',2,'{}','pending','[\"r1\"]','price')")
            db.execute('INSERT INTO price_baselines VALUES(?,?,?,?,?,?,1)',
                       ('device-1', 'workspace-1', sql_scope, '123', 50000, 'v1-result'))
            db.execute('INSERT INTO price_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('v1-result', 'v1-run', 'device-1', 'workspace-1', sql_scope,
                        '123', 1, 50000, 40000, 1, '', '{}', 2))
        migrated = ProfitMonitor(old_folder, source_provider=lambda: self.source)
        try:
            status = migrated.status()
            self.assertFalse(status['enabled'])
            self.assertFalse(status['notify'])
            self.assertEqual(status['events'], [])
            with migrated.db() as db:
                self.assertEqual(db.execute("SELECT status FROM events WHERE id='v1-event'").fetchone()[0], 'cancelled')
                row = db.execute('SELECT unit_cents,anchor_cents,anchor_result_id FROM price_baselines').fetchone()
                self.assertEqual(tuple(row), (50000, 50000, 'v1-result'))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM price_results').fetchone()[0], 1)
            self.source.cost = 59999
            migrated.check()
            with migrated.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='price-v2'").fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT unit_cents,anchor_cents FROM price_baselines').fetchone()[:], (59999, 50000))
            self.source.cost = 60000
            migrated.check()
            with migrated.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='price-v2'").fetchone()[0], 1)
        finally:
            migrated.stop()

    def test_restart_baseline_and_long_message_splitting(self):
        value = manifest()
        value['configs'][0]['name'] = '长名称' * 75
        for index in range(1, 30):
            extra = copy.deepcopy(value['configs'][0])
            extra['configId'] = f'c{index+1}'
            value['configs'].append(extra)
        self.write(rehash(value))
        self.monitor.bind(self.file)
        self.monitor.check()
        self.monitor.stop()
        self.monitor = ProfitMonitor(self.folder, source_provider=lambda: self.source)
        self.source.cost += 10000
        self.monitor.check()
        events = self.events()
        self.assertGreater(len(events), 1)
        self.assertTrue(all(len(json.loads(event['payload'])['text']['content'].encode('utf-8')) <= 12000 for event in events))
        self.assertEqual(sum(json.loads(event['payload'])['text']['content'].count('长名称') for event in events), 30 * 75)

    def test_coordinated_source_keeps_scope_with_captured_connection(self):
        import threading
        class Worker:
            lock = threading.RLock()
            busy = False
            last_call = 0
            source = None
        worker = Worker()
        class Source:
            scope = 'sql:A'
            def read_profit_costs(self, ids):
                worker.source = type('New', (), {'scope': 'sql:B'})()
                return dict(costs={'123': 1}, failed=[])
        worker.source = Source()
        result = CoordinatedProfitSource(worker).read_profit_costs({'123'})
        self.assertEqual(result['sourceScope'], 'sql:A')
        self.assertEqual(result['costs']['123'], 1)


if __name__ == '__main__':
    unittest.main()
