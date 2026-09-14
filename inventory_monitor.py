"""Transactional product configuration, snapshots, events and message association."""
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from inventory_rules import validate_product, classify, quantity
from inventory_inbound import apply_journal
from inventory_warehouses import WarehouseLookups, ConfigurationChanged, source_key, monitor_quantity, inventory_risk, select_inventory


class InventoryService(WarehouseLookups):
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'inventory.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (sku TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checks (id TEXT PRIMARY KEY, observed REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sku TEXT, data TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog (sku TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbound_seen (id TEXT PRIMARY KEY, sku TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS journal_baselines (sku TEXT PRIMARY KEY);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        try:
            with db:
                yield db
        except sqlite3.DatabaseError:
            raise ValueError('库存配置或记录无法读取，请检查数据文件，程序不会自动清空。') from None
        finally:
            db.close()

    @staticmethod
    def get(db, key, default=None):
        row = db.execute('SELECT data FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def products(self):
        with self.db() as db:
            rows = db.execute('SELECT p.data,c.data FROM products p LEFT JOIN catalog c ON p.sku=c.sku ORDER BY p.rowid')
            result = []
            for saved, cached in rows:
                product = json.loads(saved)
                if not product.get('goods_id') and cached:
                    product['goods_id'] = json.loads(cached).get('goods_id', '')
                result.append(product)
            return result

    def save_product(self, product):
        product = validate_product(product)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._save_product(db, product)

    def _save_product(self, db, product):
        """Save inside the caller transaction, including snapshot/event reconciliation."""
        old = db.execute('SELECT data FROM products WHERE sku=?', (product['sku'],)).fetchone()
        catalog = db.execute('SELECT data FROM catalog WHERE sku=?', (product['sku'],)).fetchone()
        identity = str(json.loads(catalog[0]).get('goods_id', '')) if catalog else ''
        previous_id = str(json.loads(old[0]).get('goods_id', '')) if old else ''
        known_id = identity or previous_id
        if product.get('goods_id') and known_id and product['goods_id'] != known_id:
            raise ValueError('ERP 编号与采集商品不一致，请重新搜索选择产品。')
        product['goods_id'] = known_id or product.get('goods_id', '')
        previous = json.loads(old[0]) if old else None
        self.validate_warehouse_selection(db, product, previous)
        source_changed = previous is not None and source_key(previous) != source_key(product)
        if source_changed:
            db.execute('DELETE FROM snapshots WHERE sku=?', (product['sku'],))
            db.execute('DELETE FROM journal_baselines WHERE sku=?', (product['sku'],))
            self.put(db, 'last_skus', [sku for sku in self.get(db, 'last_skus', []) if sku != product['sku']])
            for ident, data in db.execute("SELECT id,data FROM events WHERE status IN ('pending','failed')").fetchall():
                if json.loads(data).get('sku') == product['sku']:
                    db.execute("UPDATE events SET status='cancelled',retry_at=0 WHERE id=?", (ident,))
        snap = db.execute('SELECT data FROM snapshots WHERE sku=?', (product['sku'],)).fetchone()
        db.execute('INSERT INTO products VALUES (?,?) ON CONFLICT(sku) DO UPDATE SET data=excluded.data',
                   (product['sku'], json.dumps(product, ensure_ascii=False)))
        if old and snap:
            previous, snapshot = json.loads(old[0]), json.loads(snap[0])
            fresh = 0 <= time.time() - snapshot['observed_at'] <= self.get(db, 'interval_seconds', 7200) + 300
            if fresh and product['sku'] in self.get(db, 'last_skus', []) and previous['threshold'] != product['threshold'] and product['enabled']:
                self._risk_event(db, product, snapshot, snapshot, 'threshold_changed',
                                 old_risk=inventory_risk(snapshot, previous['threshold']))
                snapshot['evaluated_threshold'] = product['threshold']
                db.execute('UPDATE snapshots SET data=? WHERE sku=?', (json.dumps(snapshot), product['sku']))
        self.put(db, 'config_revision', self.get(db, 'config_revision', 0) + 1)
        self.put(db, 'next_check', 0)
        return product

    def remove_product(self, sku):
        results = self.remove_products([sku])
        return results[0] if results else None

    def remove_products(self, skus, *, expected=None):
        """Atomically remove reviewed watches; retain history and archived settings."""
        skus = list(dict.fromkeys(skus))
        if not skus:
            return []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            products = {}
            for sku in skus:
                row = db.execute('SELECT data FROM products WHERE sku=?', (sku,)).fetchone()
                product = json.loads(row[0]) if row else None
                if expected is not None and (product is None or expected.get(sku) != product):
                    raise ValueError('勾选产品的设置已变化，请刷新清单后重新勾选。')
                if product is not None:
                    products[sku] = product
            results = {sku: dict(sku=sku, name=p['name'], in_flight=0) for sku, p in products.items()}
            for sku, product in products.items():
                self.put(db, 'removed_product:' + sku, dict(product=product, removed_at=time.time()))
                db.execute('DELETE FROM products WHERE sku=?', (sku,))
            if products:
                self.put(db, 'config_revision', self.get(db, 'config_revision', 0) + 1)
                self.put(db, 'next_check', 0)
                for ident, data, status in db.execute("SELECT id,data,status FROM events WHERE status IN ('pending','failed','sending')").fetchall():
                    sku = json.loads(data).get('sku')
                    if sku not in products:
                        continue
                    if status == 'sending':
                        results[sku]['in_flight'] += 1
                    else:
                        db.execute("UPDATE events SET status='cancelled',retry_at=0 WHERE id=?", (ident,))
            return list(results.values())

    def set_enabled(self, enabled):
        with self.db() as db:
            self.put(db, 'enabled', bool(enabled))
            if enabled:
                self.put(db, 'next_check', 0)

    def set_interval(self, minutes):
        text = str(minutes).strip()
        if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 10080:
            raise ValueError('监控周期请输入 1–10080 分钟的整数。')
        seconds = int(text) * 60
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.put(db, 'interval_seconds', seconds)
            checked = self.get(db, 'checked_at', 0)
            self.put(db, 'next_check', max(time.time(), checked + seconds) if checked else 0)
        return seconds

    def request_check(self):
        with self.db() as db:
            self.put(db, 'enabled', True)
            self.put(db, 'next_check', 0)

    def connection_update(self, error=None, now=None):
        with self.db() as db:
            self.put(db, 'heartbeat', time.time() if now is None else now)
            if error is not None:
                self.put(db, 'connection_error', str(error)[:300])

    def _event(self, db, product, kind, before, after, cause):
        switch = 'inbound' if kind in ('stock_increase', 'inbound') else 'low' if kind == 'out_of_stock' else kind
        if not product['alerts'].get(switch, True):
            return
        event = dict(id=uuid.uuid4().hex, sku=product['sku'], name=product['name'], kind=kind,
                     before=before, after=after, threshold=product['threshold'], cause=cause,
                     observed_at=after['observed_at'], warehouse=product.get('warehouse_name') or '公司大库')
        db.execute('INSERT INTO events (id,data,created) VALUES (?,?,?)',
                   (event['id'], json.dumps(event, ensure_ascii=False), time.time()))

    def _risk_event(self, db, p, old, current, cause='inventory_changed', old_risk=None):
        new_risk = inventory_risk(current, p['threshold'])
        previous_threshold = old.get('evaluated_threshold', p['threshold']) if old else p['threshold']
        before = old_risk or (inventory_risk(old, previous_threshold) if old else 'normal')
        if previous_threshold != p['threshold']:
            cause = 'threshold_changed'
        if cause == 'inventory_changed' and old and before == 'low' and monitor_quantity(old) > 0 and monitor_quantity(current) == 0:
            self._event(db, p, 'out_of_stock', old, current, cause)
            return
        if before == new_risk:
            return
        kind = 'recovery' if before in ('oversold', 'negative_stock') and new_risk not in ('oversold', 'negative_stock') or new_risk == 'normal' else new_risk
        self._event(db, p, kind, old, current, cause)

    def accept_snapshot(self, check_id, rows, *, scope, warehouse='公司大库', complete=True, observed_at=None, journals=None, warehouse_details=None, config_revision=None):
        observed = time.time() if observed_at is None else quantity(observed_at)
        if not complete or warehouse != '公司大库' or not scope or not check_id or not isinstance(rows, list):
            raise ValueError('库存检查不完整或公司大库身份不匹配，已保留上次数据。')
        mapped = {}
        if warehouse_details is not None and not isinstance(warehouse_details, dict):
            raise ValueError('分库明细格式无效。')
        for row in rows:
            sku = str(row.get('sku', '')).strip()
            if not sku or sku in mapped:
                raise ValueError('库存商品编码缺失或重复，无法建立可靠快照。')
            if row.get('stock_unknown') is True:
                if row.get('able') is not None:
                    raise ValueError('未知库存标记与数量冲突。')
                mapped[sku] = dict(sku=sku, name=str(row.get('name', ''))[:400],
                                   goods_id=str(row.get('goods_id', '')), able=None, purchase=None,
                                   stock=None, stock_unknown=True, observed_at=observed)
                for key in ('purchase', 'stock'):
                    mapped[sku][key] = None if row.get(key) is None else quantity(row[key])
                continue
            mapped[sku] = dict(sku=sku, name=str(row.get('name', ''))[:400],
                               able=quantity(row.get('able')), purchase=quantity(row.get('purchase')),
                               stock=quantity(row.get('stock')), observed_at=observed)
            if row.get('goods_id'):
                mapped[sku]['goods_id'] = str(row['goods_id'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if config_revision is not None and config_revision != self.get(db, 'config_revision', 0):
                raise ConfigurationChanged('产品设置已更新，请按新设置重新采集。')
            if db.execute('SELECT 1 FROM checks WHERE id=?', (check_id,)).fetchone():
                return False
            saved_scope = self.get(db, 'scope')
            if saved_scope and saved_scope != scope:
                raise ValueError('ERP 账号或仓库发生变化，请恢复原账号后继续监控。')
            if observed < self.get(db, 'checked_at', 0):
                raise ValueError('库存快照早于上次检查，已拒绝过期数据。')
            valid_skus = {sku for sku, row in mapped.items() if not row.get('stock_unknown')}
            for (data,) in db.execute('SELECT data FROM products').fetchall():
                p = json.loads(data)
                if not p['enabled'] or p['sku'] not in mapped:
                    continue
                current = select_inventory(p, mapped[p['sku']], (warehouse_details or {}).get(p['sku']))
                if current is None:
                    valid_skus.discard(p['sku'])
                    continue
                valid_skus.add(p['sku'])
                old = db.execute('SELECT data FROM snapshots WHERE sku=?', (p['sku'],)).fetchone()
                old = json.loads(old[0]) if old else None
                receipt_emitted = False
                special = p.get('stock_basis') == 'warehouse_stock'
                if journals is not None and not special:
                    if not isinstance(journals, dict) or p['sku'] not in journals:
                        raise ValueError('重点产品入库流水缺失，本轮检查未完成。')
                    receipt_emitted = apply_journal(db, p, current, old, journals[p['sku']], self._event)
                self._risk_event(db, p, old, current)
                stock_field = 'warehouse_stock' if special else 'stock'
                if not receipt_emitted and old and current.get(stock_field) is not None and old.get(stock_field) is not None and current[stock_field] > old[stock_field]:
                    self._event(db, p, 'stock_increase', old, current, 'inventory_changed')
                if not special and current['purchase'] > 0 and (old is None or current['purchase'] > old['purchase']):
                    self._event(db, p, 'pending_inbound', old, current, 'purchase_changed')
                current['evaluated_threshold'] = p['threshold']
                db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)', (p['sku'], json.dumps(current, ensure_ascii=False)))
            # Catalog is a lookup aid; missing watched products stay unknown in this check.
            db.execute('DELETE FROM catalog')
            db.executemany('INSERT INTO catalog VALUES (?,?)', [(sku, json.dumps(r, ensure_ascii=False)) for sku, r in mapped.items()])
            db.execute('INSERT INTO checks VALUES (?,?)', (check_id, observed))
            for key, value in dict(scope=scope, checked_at=observed, next_check=observed+self.get(db, 'interval_seconds', 7200),
                                   last_skus=sorted(valid_skus), heartbeat=time.time(), connection_error='',
                                   inbound_verified=journals is not None).items():
                self.put(db, key, value)
        return True

    def catalog(self, query='', limit=100, mode='all'):
        with self.db() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM catalog')]
        if mode == 'goods_id':
            return [r for r in rows if not query.strip() or str(r.get('goods_id', '')) == query.strip()][:limit]
        if mode not in ('name', 'all'):
            raise ValueError('未知搜索模式')
        terms = query.casefold().split()
        if mode == 'name':
            return [r for r in rows if all(t in r['name'].casefold() for t in terms)][:limit]
        return [r for r in rows if all(t in (r['name'] + ' ' + r['sku']).casefold() for t in terms)][:limit]

    def status(self):
        from notification_robots import store_for_service
        robots = [r for r in store_for_service(self).list_robots() if 'inventory' in r['channels']]
        robot_status = dict(name='多机器人', kind='robot', configured=bool(robots), count=len(robots),
                            enabled_count=sum(r['enabled'] for r in robots))
        now = time.time()
        with self.db() as db:
            products = self.products()
            snapshots = {r[0]: json.loads(r[1]) for r in db.execute('SELECT sku,data FROM snapshots')}
            heartbeat, error = self.get(db, 'heartbeat', 0), self.get(db, 'connection_error', '')
            checked = self.get(db, 'checked_at', 0)
            present = self.get(db, 'last_skus', [])
            for p in products:
                p.setdefault('stock_basis', 'company_able')
                p.setdefault('warehouse_id', '')
                p.setdefault('warehouse_name', '')
                p.update(able=None, purchase=None, stock=None, monitor_qty=None, observed_at=0, risk='unknown')
                if p['sku'] in snapshots:
                    saved_name = p['name']
                    p.update(snapshots[p['sku']])
                    p['name'] = saved_name
                    p['monitor_qty'] = monitor_quantity(snapshots[p['sku']])
                    p['risk'] = inventory_risk(p, p['threshold']) if p['sku'] in present else 'unknown'
                if not p['enabled']:
                    p['risk'] = 'disabled'
            events = []
            for data, delivery in db.execute('SELECT data,status FROM events ORDER BY created DESC,rowid DESC LIMIT 100'):
                events.append(dict(json.loads(data), delivery_status=delivery))
            return dict(enabled=self.get(db, 'enabled', True), interval_seconds=self.get(db, 'interval_seconds', 7200),
                        connection='error' if error else ('online' if now-heartbeat < 70 else ('stale' if heartbeat else 'waiting')),
                        connection_error=error, checked_at=checked, next_check=self.get(db, 'next_check', 0),
                        products=products, events=events, destination=self.get(db, 'destination', None),
                        robot_destination=robot_status,
                        delivery_error=self.get(db, 'delivery_error', ''),
                        inbound_verified=self.get(db, 'inbound_verified', False))

    def record_message(self, message):
        text = str(message.get('text', ''))[:10000]
        ident = str(message.get('message_id', ''))
        if not ident or not text:
            return
        try:
            created = quantity(message.get('created_at') or 0)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(str(message['created_at']).replace('Z','+00:00'))
                created = parsed.replace(tzinfo=parsed.tzinfo or timezone(timedelta(hours=8))).timestamp()
            except (TypeError, ValueError, KeyError, OverflowError):
                created = 0
        if created > 1e11:
            created /= 1000
        products = [p for p in self.products() if p['enabled']]
        matches = []
        for p in products:
            terms = [p['sku']] + [t.strip() for t in p['keywords'].replace('，', ',').split(',') if t.strip()]
            if any(re.search(r'(?<![A-Za-z0-9])'+re.escape(t)+r'(?![A-Za-z0-9])', text, re.I) for t in terms):
                matches.append(p['sku'])
        data = dict(message_id=ident, text=text, sender=str(message.get('sender', ''))[:100],
                    group=str(message.get('group', ''))[:200], created_at=created,
                    group_id=str(message.get('group_id', ''))[:300], sender_id=str(message.get('sender_id', ''))[:300])
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?)',
                       (ident, matches[0] if len(matches)==1 else None, json.dumps(data, ensure_ascii=False), created))

    def related_messages(self, sku, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT data FROM messages WHERE sku=? AND created BETWEEN ? AND ? ORDER BY created DESC LIMIT 3',
                (sku, now-7*86400, now))]
