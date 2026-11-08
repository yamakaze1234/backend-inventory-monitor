"""Read-only ERP unit-cost watch driven by the workbench's complete manifest."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from profit_manifest import canonical, read_manifest, sha
from price_schedule import normalize_times, next_daily

INTERVAL_SECONDS = 120 * 60
PRICE_MODE = 'erp-unit-price/v2'
PRICE_SOURCE_SCOPE = 'erp-unit-price/v1'  # Keep SQL-source identity stable across the alert-rule upgrade.
PRICE_EVENT_KIND = 'price-v2'
CHANGE_CENTS = 10000
PRICE_BASIS = ('库存.分库库存', '公司大库', '库存成本', '元转整数分:ROUND_HALF_UP')
VALID_ID = re.compile(r'[1-9][0-9]{0,19}\Z')


class CoordinatedProfitSource:
    """Capture one SQL source and its identity under the inventory worker lock."""
    def __init__(self, worker):
        self.worker = worker

    def read_profit_costs(self, ids):
        worker = self.worker
        with worker.lock:
            if worker.source is None or worker.busy or time.time() < worker.last_call + 30:
                raise ValueError('SQL 连接未就绪或库存采集正在运行')
            source = worker.source
            source_scope = source.scope
            worker.busy = True
            worker.last_call = time.time()
        try:
            result = source.read_profit_costs(ids)
            return {**result, 'sourceScope': source_scope, 'collectedAt': time.time()}
        finally:
            with worker.lock:
                worker.busy = False


def watched_goods(manifest):
    """One exact goods_id with every affected saved configuration."""
    goods = {}
    invalid = 0
    for config in manifest['configs']:
        for part in config['parts']:
            gid = part['goodsId']
            if not VALID_ID.fullmatch(gid):
                invalid += 1
                continue
            item = goods.setdefault(gid, dict(names=set(), refs=set()))
            if part['name']:
                item['names'].add(part['name'])
            item['refs'].add((config['shopId'], config['configId'], config['product'], config['name'], part['slot']))
    return {gid: dict(names=sorted(value['names']), refs=sorted(value['refs']))
            for gid, value in goods.items()}, invalid


def affected_config_count(refs):
    return len({(shop, config_id) for shop, config_id, *_ in refs})


class ProfitMonitor:
    """Keep the old API/channel key; all new checks use ERP unit prices only."""
    def __init__(self, root, source_provider=None, robots=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'profit_monitor.sqlite3'
        self.source_provider = source_provider
        self.robots = robots
        self.lock = threading.RLock()
        self.running = False
        self.run_thread_ident = None
        self.stop_event = threading.Event()
        self.thread = None
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS snapshot(id INTEGER PRIMARY KEY CHECK(id=1),content TEXT NOT NULL,received REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,source_id TEXT,revision INTEGER,started REAL,finished REAL,status TEXT,error TEXT);
              CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,shop_id TEXT,created REAL,payload TEXT,status TEXT,recipients TEXT NOT NULL DEFAULT '[]');
              CREATE TABLE IF NOT EXISTS price_baselines(
                  source_id TEXT NOT NULL,workspace_id TEXT NOT NULL,sql_scope TEXT NOT NULL,goods_id TEXT NOT NULL,
                  unit_cents INTEGER NOT NULL,last_result_id TEXT NOT NULL,active INTEGER NOT NULL,
                  anchor_cents INTEGER,anchor_result_id TEXT,
                  PRIMARY KEY(source_id,workspace_id,sql_scope,goods_id));
              CREATE TABLE IF NOT EXISTS price_results(
                  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,source_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
                  sql_scope TEXT,goods_id TEXT NOT NULL,valid INTEGER NOT NULL,unit_cents INTEGER,
                  previous_cents INTEGER,changed INTEGER NOT NULL,reason TEXT NOT NULL,details TEXT NOT NULL,created REAL NOT NULL,
                  anchor_cents INTEGER,anchor_delta_cents INTEGER,
                  UNIQUE(run_id,goods_id));
            ''')
            if 'recipients' not in [row[1] for row in db.execute('PRAGMA table_info(events)')]:
                db.execute("ALTER TABLE events ADD COLUMN recipients TEXT NOT NULL DEFAULT '[]'")
            if 'kind' not in [row[1] for row in db.execute('PRAGMA table_info(events)')]:
                db.execute("ALTER TABLE events ADD COLUMN kind TEXT NOT NULL DEFAULT 'profit'")
            if 'kind' not in [row[1] for row in db.execute('PRAGMA table_info(runs)')]:
                db.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'profit'")
            baseline_fields = {row[1] for row in db.execute('PRAGMA table_info(price_baselines)')}
            if 'anchor_cents' not in baseline_fields:
                db.execute('ALTER TABLE price_baselines ADD COLUMN anchor_cents INTEGER')
            if 'anchor_result_id' not in baseline_fields:
                db.execute('ALTER TABLE price_baselines ADD COLUMN anchor_result_id TEXT')
            result_fields = {row[1] for row in db.execute('PRAGMA table_info(price_results)')}
            if 'anchor_cents' not in result_fields:
                db.execute('ALTER TABLE price_results ADD COLUMN anchor_cents INTEGER')
            if 'anchor_delta_cents' not in result_fields:
                db.execute('ALTER TABLE price_results ADD COLUMN anchor_delta_cents INTEGER')
            db.execute('UPDATE price_baselines SET anchor_cents=unit_cents,anchor_result_id=last_result_id WHERE anchor_cents IS NULL')
            db.execute("INSERT OR IGNORE INTO settings VALUES('interval','7200')")
            db.execute("INSERT OR IGNORE INTO settings VALUES('enabled','false')")
            db.execute("INSERT OR IGNORE INTO settings VALUES('notify','false')")
            db.execute("UPDATE runs SET status='interrupted',finished=? WHERE status='running'", (time.time(),))
            if self._get(db, 'monitor_mode') != PRICE_MODE:
                # Historical profit results and receipts stay readable, but cannot be delivered in price mode.
                db.execute("UPDATE events SET status='cancelled' WHERE kind IN ('profit','price') AND status IN ('pending','failed','partial')")
                self._set(db, 'enabled', False)
                self._set(db, 'notify', False)
                self._set(db, 'next_check', 0)
                self._set(db, 'monitor_mode', PRICE_MODE)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _get(self, db, key, default=None):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, db, key, value):
        db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, canonical(value)))

    def preview(self, path):
        manifest = read_manifest(path)
        goods, invalid = watched_goods(manifest)
        return dict(sourceId=manifest['sourceId'], workspaceId=manifest['workspaceId'],
                    revision=manifest['revision'], publishedAt=manifest['publishedAt'],
                    count=len(manifest['configs']), goodsCount=len(goods), invalidIds=invalid,
                    shops=sorted({c['shopId'] for c in manifest['configs']}), path=str(Path(path).resolve()))

    def bind_maintenance(self, sources, *, expected_preview=None):
        # 维护表快照只存入本地监控库，不回写工作簿。
        return self.bind('', maintenance_sources=sources, expected_preview=expected_preview)

    def bind(self, path, *, maintenance_sources=None, expected_preview=None):
        if maintenance_sources is not None:
            from price_maintenance import read_sources, normalize_sources
            maintenance_sources = normalize_sources(maintenance_sources)
            with self.db() as db:
                row = db.execute('SELECT content FROM snapshot WHERE id=1').fetchone()
            manifest = read_sources(maintenance_sources, previous=json.loads(row[0]) if row else None, expected=expected_preview)
        else:
            manifest = read_manifest(path)
        with self.lock, self.db() as db:
            if self.running and threading.get_ident() != self.run_thread_ident:
                raise ValueError('检测正在运行，请本轮完成后重新绑定')
            current = db.execute('SELECT content FROM snapshot WHERE id=1').fetchone()
            if current:
                old = json.loads(current[0])
                if (old['sourceId'], old['workspaceId']) != (manifest['sourceId'], manifest['workspaceId']):
                    raise ValueError('联动来源或工作区已变化，请先解除旧绑定')
                if (manifest['revision'] < old['revision'] or
                    manifest['revision'] == old['revision'] and manifest['businessHash'] != old['businessHash']):
                    raise ValueError('联动修订回退或同修订内容不同，保留旧快照')
                old_ids = set(watched_goods(old)[0])
                current_ids = set(watched_goods(manifest)[0])
                for removed_id in old_ids - current_ids:
                    db.execute('UPDATE price_baselines SET active=0 WHERE source_id=? AND workspace_id=? AND goods_id=?',
                               (manifest['sourceId'], manifest['workspaceId'], removed_id))
            db.execute('INSERT INTO snapshot VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,received=excluded.received',
                       (canonical(manifest), time.time()))
            self._set(db, 'manifest_path', str(Path(path).resolve()) if maintenance_sources is None else '')
            self._set(db, 'maintenance_sources', maintenance_sources or [])
            self._set(db, 'last_read_at', time.time())
            self._set(db, 'source_error', '')
        if maintenance_sources is not None:
            return dict(count=len(manifest['configs']),goodsCount=len(watched_goods(manifest)[0]))
        return self.preview(path)

    def refresh(self):
        with self.db() as db:
            path = self._get(db, 'manifest_path')
            sources = self._get(db, 'maintenance_sources', [])
        if not path and not sources:
            raise ValueError('尚未绑定联动快照')
        try:
            return self.bind_maintenance(sources) if sources else self.bind(path)
        except Exception as error:
            with self.db() as db:
                self._set(db, 'source_error', str(error)[:200])
            raise

    def unbind(self):
        with self.lock:
            if self.running:
                raise ValueError('检测正在运行，请完成后解除绑定')
            with self.db() as db:
                row = db.execute('SELECT content FROM snapshot WHERE id=1').fetchone()
                if row:
                    manifest = json.loads(row[0])
                    db.execute('UPDATE price_baselines SET active=0 WHERE source_id=? AND workspace_id=?',
                               (manifest['sourceId'], manifest['workspaceId']))
                db.execute('DELETE FROM snapshot WHERE id=1')
                db.execute("UPDATE events SET status='cancelled' WHERE status IN ('pending','partial','failed')")
                self._set(db, 'manifest_path', '')
                self._set(db, 'maintenance_sources', [])
                self._set(db, 'enabled', False)
                self._set(db, 'notify', False)
                self._set(db, 'next_check', 0)
                self._set(db, 'source_error', '')

    def set_enabled(self, enabled):
        with self.db() as db:
            if enabled and not db.execute('SELECT 1 FROM snapshot WHERE id=1').fetchone():
                raise ValueError('请先绑定完整联动快照')
            self._set(db, 'enabled', bool(enabled))
            if enabled:
                self._set(db, 'next_check', self._next_check(db) if self._get(db, 'schedule_mode') == 'daily' else 0)

    def set_schedule(self, times, enabled):
        # 全部校验通过后一次保存，避免无效时间误开启巡检。
        times = normalize_times(times)
        with self.lock, self.db() as db:
            if enabled and not db.execute('SELECT 1 FROM snapshot WHERE id=1').fetchone():
                raise ValueError('请先绑定主机维护表或完整配置快照')
            self._set(db, 'schedule_mode', 'daily')
            self._set(db, 'schedule_times', times)
            self._set(db, 'schedule_failures', 0)
            self._set(db, 'enabled', bool(enabled))
            self._set(db, 'next_check', next_daily(time.time(), times) if enabled else 0)

    def _schedule_failure(self, db, error):
        # SQL 忙或连接暂不可用不应占掉当天唯一巡检；最多补试三次。
        attempt = self._get(db, 'schedule_failures', 0) + 1
        self._set(db, 'schedule_failures', attempt)
        self._set(db, 'last_error', error)
        self._set(db, 'next_check', time.time()+300 if attempt <= 3 else self._next_check(db))
        if attempt > 3:self._set(db, 'schedule_failures', 0)

    def _next_check(self, db):
        # 旧版间隔配置保持兼容；保存新表单后才切换固定时刻。
        if self._get(db, 'schedule_mode', 'interval') == 'daily':
            return next_daily(time.time(), self._get(db, 'schedule_times', ['09:00']))
        return time.time() + self._get(db, 'interval', INTERVAL_SECONDS)

    def set_interval(self, minutes):
        minutes = int(minutes)
        if not 10 <= minutes <= 10080:
            raise ValueError('检测周期需为 10 至 10080 分钟')
        with self.db() as db:
            self._set(db, 'interval', minutes * 60)

    def set_notifications(self, enabled):
        if enabled and (self.robots is None or not any(r['enabled'] and 'profit' in r['channels'] for r in self.robots.list_robots())):
            raise ValueError('请先明确选择并启用 ERP 单价变化提醒机器人')
        with self.db() as db:
            self._set(db, 'notify', bool(enabled))

    def status(self):
        with self.db() as db:
            row = db.execute('SELECT content FROM snapshot WHERE id=1').fetchone()
            manifest = json.loads(row[0]) if row else None
            goods, invalid = watched_goods(manifest) if manifest else ({}, 0)
            last = db.execute('SELECT * FROM runs WHERE kind=? ORDER BY started DESC LIMIT 1', (PRICE_EVENT_KIND,)).fetchone()
            events = [dict(r) for r in db.execute('SELECT id,shop_id,created,status FROM events WHERE kind=? ORDER BY created DESC LIMIT 30', (PRICE_EVENT_KIND,))]
            results = [dict(r) for r in db.execute('SELECT * FROM price_results WHERE run_id=? ORDER BY goods_id LIMIT 200',
                                                  (last['id'],))] if last else []
            for result in results:
                details = json.loads(result['details'])
                result['name'] = '、'.join(details.get('names', [])[:2]) or '未命名配件'
                result['affectedConfigs'] = affected_config_count(details.get('refs', []))
                result['refs'] = details.get('refs', [])
            return dict(bound=bool(row), path=self._get(db, 'manifest_path', ''),
                        maintenanceSources=self._get(db, 'maintenance_sources', []),
                        sourceId=manifest['sourceId'] if manifest else None,
                        revision=manifest['revision'] if manifest else None,
                        count=len(manifest['configs']) if manifest else 0, goodsCount=len(goods), invalidIds=invalid,
                        publishedAt=manifest['publishedAt'] if manifest else None,
                        lastReadAt=self._get(db, 'last_read_at'), sourceError=self._get(db, 'source_error', ''),
                        enabled=self._get(db, 'enabled', False), notify=self._get(db, 'notify', False),
                        intervalSeconds=self._get(db, 'interval', INTERVAL_SECONDS),
                        scheduleMode=self._get(db, 'schedule_mode', 'interval'),
                        scheduleTimes=self._get(db, 'schedule_times', ['09:00']),
                        nextCheck=self._get(db, 'next_check', 0), running=self.running,
                        lastError=self._get(db, 'last_error', ''),
                        lastRun=dict(last) if last else None, lastResults=results, events=events)

    def notification_preview(self):
        recipients = [r['name'] for r in self.robots.list_robots() if r['enabled'] and 'profit' in r['channels']] if self.robots else []
        with self.db() as db:
            row = db.execute('SELECT payload FROM events WHERE kind=? ORDER BY created DESC LIMIT 1', (PRICE_EVENT_KIND,)).fetchone()
        content = json.loads(row[0])['text']['content'] if row else '尚无 ERP 单价变化事件。首次有效采集仅建立基准；相对上次提醒基准累计涨跌达到 100 元才提醒。'
        return dict(recipients=recipients, message=content)

    def check(self, *, allow_notifications=True):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.run_thread_ident = threading.get_ident()
        run_id = None
        try:
            try:
                self.refresh()
            except Exception:
                # 维护表刷新失败时不能拿旧配件范围冒充本轮读取成功。
                with self.db() as db:
                    if self._get(db, 'maintenance_sources', []):raise
                pass  # 旧版 JSON 绑定保留兼容的最后完整快照。
            with self.db() as db:
                row = db.execute('SELECT content FROM snapshot WHERE id=1').fetchone()
                if not row:
                    raise ValueError('尚未绑定联动快照')
                manifest = json.loads(row[0])
                run_id = uuid.uuid4().hex
                db.execute('INSERT INTO runs(id,source_id,revision,started,status,kind) VALUES(?,?,?,?,?,?)',
                           (run_id, manifest['sourceId'], manifest['revision'], time.time(), 'running', PRICE_EVENT_KIND))
            watch, invalid = watched_goods(manifest)
            # 保存本批次实际 BOM 数量，历史比较不借用后续修改的配置。
            quantities={}
            for config in manifest['configs']:
                counts={}
                for part in config['parts']:
                    gid=part['goodsId'];qty=part['qty']
                    counts[gid]=None if qty is None or counts.get(gid,0) is None else counts.get(gid,0)+qty
                for gid,qty in counts.items():quantities.setdefault(gid,[]).append([config['shopId'],config['configId'],qty])
            ids = set(watch)
            costs, failed, source_scope, cost_error, cost_at = {}, set(), None, '', time.time()
            try:
                source = self.source_provider() if self.source_provider else None
                if source is None:
                    raise ValueError('SQL 成本连接尚未启动')
                collected = source.read_profit_costs(ids)
                source_scope = collected['sourceScope']
                if not isinstance(source_scope, str) or not source_scope:
                    raise ValueError('SQL 成本来源身份缺失')
                costs = collected['costs']
                failed = set(collected.get('failed', []))
                cost_at = collected.get('collectedAt', time.time())
                if (not isinstance(costs, dict) or not isinstance(collected.get('failed', []), list)
                    or not set(costs) <= ids or not failed <= ids):
                    raise ValueError('SQL 成本查询范围无效')
                # SQL 适配器以 failed 返回失败批次；查询成功但缺价格的 ID 不算传输失败。
                if failed:cost_error = '部分 ERP 成本批次查询失败，等待补试'
            except Exception:
                costs, failed, source_scope = {}, ids, None
                cost_error = 'ERP 成本采集失败或连接未启用'
            sql_scope = sha([PRICE_SOURCE_SCOPE, source_scope, *PRICE_BASIS]) if source_scope else None
            with self.db() as db:
                scope_key = [manifest['sourceId'], manifest['workspaceId']]
                previous_scope = self._get(db, 'price_sql_scope')
                if sql_scope and previous_scope != sql_scope:
                    db.execute('UPDATE price_baselines SET active=0 WHERE source_id=? AND workspace_id=?', scope_key)
                    self._set(db, 'price_sql_scope', sql_scope)
                if ids:
                    placeholders = ','.join('?' for _ in ids)
                    db.execute(f'UPDATE price_baselines SET active=0 WHERE source_id=? AND workspace_id=? AND goods_id NOT IN ({placeholders})',
                               (*scope_key, *sorted(ids)))
                else:
                    db.execute('UPDATE price_baselines SET active=0 WHERE source_id=? AND workspace_id=?', scope_key)
                notify = allow_notifications and self._get(db, 'notify', False)
                recipients = [r['id'] for r in self.robots.list_robots() if r['enabled'] and 'profit' in r['channels']] if notify and self.robots else []
                for gid in sorted(ids):
                    old = db.execute('''SELECT * FROM price_baselines WHERE source_id=? AND workspace_id=?
                        AND sql_scope=? AND goods_id=? AND active=1''', (*scope_key, sql_scope, gid)).fetchone() if sql_scope else None
                    unit = costs.get(gid)
                    valid = (sql_scope is not None and gid not in failed and type(unit) is int and unit >= 0)
                    reason = '' if valid else (cost_error or '该商品 ERP 单价缺失或无效')
                    prior = old['unit_cents'] if old else None
                    anchor = old['anchor_cents'] if old else None
                    anchor_delta = unit-anchor if valid and anchor is not None else None
                    changed = bool(valid and old and abs(anchor_delta) >= CHANGE_CENTS)
                    result_id = uuid.uuid4().hex
                    details = dict(names=watch[gid]['names'], refs=watch[gid]['refs'], publishedAt=manifest['publishedAt'],
                                   sourceId=manifest['sourceId'], workspaceId=manifest['workspaceId'],
                                   sourceRevision=manifest['revision'], sqlScope=sql_scope, costAt=cost_at,
                                   basis=PRICE_BASIS, failedBatch=gid in failed, configQuantities=quantities.get(gid,[]))
                    db.execute('''INSERT INTO price_results
                        (id,run_id,source_id,workspace_id,sql_scope,goods_id,valid,unit_cents,previous_cents,
                         changed,reason,details,created,anchor_cents,anchor_delta_cents)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (result_id, run_id, *scope_key, sql_scope, gid, int(valid), unit if valid else None,
                         prior, int(changed), reason, canonical(details), time.time(), anchor, anchor_delta))
                    if valid:
                        next_anchor = unit if not old or changed and allow_notifications else anchor
                        next_anchor_id = result_id if not old or changed and allow_notifications else old['anchor_result_id']
                        db.execute('''INSERT INTO price_baselines
                            (source_id,workspace_id,sql_scope,goods_id,unit_cents,last_result_id,active,anchor_cents,anchor_result_id)
                            VALUES(?,?,?,?,?,?,1,?,?)
                            ON CONFLICT(source_id,workspace_id,sql_scope,goods_id) DO UPDATE SET
                            unit_cents=excluded.unit_cents,last_result_id=excluded.last_result_id,active=1,
                            anchor_cents=excluded.anchor_cents,anchor_result_id=excluded.anchor_result_id''',
                                   (*scope_key, sql_scope, gid, unit, result_id, next_anchor, next_anchor_id))
                    if changed and allow_notifications:
                        self._insert_change_events(db, run_id, manifest, gid, watch[gid], anchor, unit, cost_at, recipients)
                db.execute('UPDATE runs SET status=?,finished=?,error=? WHERE id=?',
                           ('failed' if cost_error else 'completed', time.time(), cost_error or None, run_id))
                if allow_notifications:
                    if cost_error:self._schedule_failure(db, cost_error)
                    else:
                        self._set(db, 'schedule_failures', 0)
                        self._set(db, 'last_error', '')
                        self._set(db, 'next_check', self._next_check(db))
                self._set(db, 'invalid_ids', invalid)
            if self.robots and allow_notifications:
                self.deliver_pending()
            return run_id
        except Exception as error:
            with self.db() as db:
                if run_id:
                    db.execute("UPDATE runs SET status='failed',finished=?,error=? WHERE id=? AND status='running'",
                               (time.time(), str(error)[:200], run_id))
            raise
        finally:
            with self.lock:
                self.running = False
                self.run_thread_ident = None

    def _price_message(self, manifest, gid, info, before, after, cost_at, refs):
        direction = '上涨' if after > before else '下降'
        lines = [f'ERP 单价累计变化达到 100 元 · goods_id {gid}',
                 f'配件：{"、".join(info["names"][:3]) or "未命名配件"}',
                 f'公司大库库存成本：上次提醒基准 {before/100:.2f} → 当前 {after/100:.2f} 元（累计{direction} {abs(after-before)/100:.2f} 元）',
                 f'成本采集：{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cost_at))}',
                 f'配置发布：{manifest["publishedAt"]}',
                 f'受影响配置：{affected_config_count(info["refs"])} 套']
        lines.extend(f'  {shop} / {product} / {name} / {slot}' for shop, _, product, name, slot in refs)
        return '\n'.join(lines)

    def _insert_change_events(self, db, run_id, manifest, gid, info, before, after, cost_at, recipients):
        chunks, pending = [], []
        for ref in info['refs']:
            candidate = pending + [ref]
            if pending and len(self._price_message(manifest, gid, info, before, after, cost_at, candidate).encode('utf-8')) > 12000:
                chunks.append(pending)
                pending = [ref]
            else:
                pending = candidate
            if len(self._price_message(manifest, gid, info, before, after, cost_at, pending).encode('utf-8')) > 12000:
                raise ValueError('单个受影响配置超过消息上限')
        if pending:
            chunks.append(pending)
        for index, refs in enumerate(chunks):
            event_id = sha([PRICE_MODE, run_id, gid, index, before, after])
            payload = canonical(dict(msgtype='text', text=dict(content=self._price_message(manifest, gid, info, before, after, cost_at, refs))))
            db.execute('''INSERT INTO events(id,shop_id,created,payload,status,recipients,kind)
                VALUES(?,?,?,?,?,?,?)''',
                (event_id, 'ERP单价', time.time(), payload, 'pending' if recipients else 'skipped', canonical(recipients), PRICE_EVENT_KIND))

    def deliver_pending(self):
        with self.db() as db:
            if not self._get(db, 'notify', False):
                return []
            rows = [dict(r) for r in db.execute("SELECT id,payload,created,recipients FROM events WHERE kind=? AND status IN ('pending','failed','partial') ORDER BY created", (PRICE_EVENT_KIND,))]
        receipts = []
        for event in rows:
            result = self.robots.send('profit', event['id'], json.loads(event['payload']), event['created'],
                                      recipient_ids=json.loads(event['recipients']))
            with self.db() as db:
                db.execute('UPDATE events SET status=? WHERE id=?', (result['status'], event['id']))
            receipts.append(result)
        return receipts

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def loop():
            last_refresh = 0
            while not self.stop_event.wait(10):
                status = self.status()
                if status['enabled'] and status['scheduleMode'] != 'daily' and time.time()-last_refresh >= 30:
                    last_refresh = time.time()
                    with self.db() as db:
                        old = db.execute('SELECT content FROM snapshot WHERE id=1').fetchone()
                        old_hash = json.loads(old[0])['businessHash'] if old else None
                    try:
                        self.refresh()
                        with self.db() as db:
                            current = db.execute('SELECT content FROM snapshot WHERE id=1').fetchone()
                            new_hash = json.loads(current[0])['businessHash'] if current else None
                            if old_hash != new_hash:
                                self._set(db, 'next_check', 0)
                    except Exception:
                        pass
                    status = self.status()
                if status['enabled'] and time.time() >= status['nextCheck'] and not self.running:
                    try:
                        self.check()
                    except Exception as error:
                        with self.db() as db:
                            self._schedule_failure(db, str(error)[:200])
                elif status['notify'] and status['events']:
                    try:
                        self.deliver_pending()
                    except Exception:
                        pass
        self.thread = threading.Thread(target=loop, name='erp-price-monitor', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
