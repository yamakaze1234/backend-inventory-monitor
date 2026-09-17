"""SQL inventory source service; delivery remains owned by the existing application."""
import json
import threading
import time
import uuid
from inventory_monitor import InventoryService
from sql_inventory_source import normalize, normalize_bundle, warehouse_key


def eligible_product_name(name):
    return not any(marker in str(name or '') for marker in ('停售', '套餐', '*', '＊'))


class SqlInventoryService(InventoryService):
    _sql_session_ready = False
    _sql_session_error = ''

    def connection_update(self, error=None, now=None):
        super().connection_update(error=error, now=now)
        if error is not None:
            self._sql_session_error = str(error)
            self._sql_session_ready = not bool(error)

    def preview_events(self, *, kinds=None, warehouse='', query='', since=0, limit=200):
        """Filter persisted local history before limiting displayed results."""
        rows, total = [], 0
        query = query.strip().casefold()
        with self.db() as db:
            for data, delivery in db.execute("SELECT data,status FROM events WHERE created>=? ORDER BY created DESC,rowid DESC", (since,)):
                event = json.loads(data)
                current = event.get('after') or {}
                depot = event.get('warehouse') or current.get('warehouse_name') or '公司大库'
                if kinds and event['kind'] not in kinds:
                    continue
                if warehouse and depot != warehouse:
                    continue
                haystack = ' '.join(str(value or '') for value in (event.get('name'),event.get('sku'),current.get('goods_id'),depot))
                if query and query not in haystack.casefold():
                    continue
                total += 1
                if len(rows) < limit:
                    rows.append(dict(event, delivery_status=delivery))
        return rows, total

    def catalog(self, query='', limit=100, mode='all'):
        rows = super().catalog(query, limit=None, mode=mode)
        return self.filter_catalog(rows)[:limit]

    @staticmethod
    def filter_catalog(rows):
        return [row for row in rows if eligible_product_name(row.get('name'))]

    @staticmethod
    def _ensure_addable(db, product):
        cached = db.execute('SELECT data FROM catalog WHERE sku=?', (product.get('sku'),)).fetchone()
        names = [product.get('name')]
        if cached:
            names.append(json.loads(cached[0]).get('name'))
        if not all(eligible_product_name(name) for name in names):
            raise ValueError('停售、套餐或名称含星号的商品不加入监控。')

    def _save_product(self, db, product):
        if not db.execute('SELECT 1 FROM products WHERE sku=?', (product.get('sku'),)).fetchone():
            self._ensure_addable(db, product)
            db.execute('DELETE FROM meta WHERE key=?', ('sql_pending:' + product['sku'],))
        return super()._save_product(db, product)

    def _event(self, db, product, kind, before, after, cause):
        if kind == 'pending_inbound' and str(self.get(db, 'scope', '')).startswith('sql:'):
            return  # SQL pending has its own baseline, including products without stock rows.
        if product['sku'] in self.get(db, 'source_baseline_skus', []):
            return
        super()._event(db, product, kind, before, after, cause)

    def accept_auxiliary_observations(self, db, rows, details, scope):
        if not scope.startswith('sql:'):
            return
        for raw, in db.execute('SELECT data FROM products').fetchall():
            product = json.loads(raw)
            row = rows.get(product['sku'])
            if not product['enabled'] or not row or row.get('goods_id') != product.get('goods_id'):
                continue
            target = product.get('warehouse_name') if product.get('stock_basis') == 'warehouse_stock' else '公司大库'
            depot = next((d for d in details.get(product['sku'], {}).get('depots', []) if d['name'] == target), None)
            quantity = depot.get('purchase') if depot else None
            mode = 'warehouse'
            if quantity is None:
                mode, quantity = 'erp_total', row.get('catalog_purchase')
            key = 'sql_pending:' + product['sku']
            previous = self.get(db, key)
            identity = [scope, product['goods_id'], product.get('stock_basis', 'company_able'), target]
            current = dict(identity=identity, mode=mode, quantity=quantity, observed_at=row['observed_at'])
            if quantity is None:
                # Display unavailable data explicitly; retain the last comparable baseline.
                self.put(db, key, dict(previous or current, available=False, checked_at=row['observed_at']))
                continue
            comparable = (previous and previous.get('identity') == identity and previous.get('mode') == mode
                          and previous.get('quantity') is not None)
            if comparable and quantity > 0 and quantity > previous['quantity']:
                special = product.get('stock_basis') == 'warehouse_stock'
                def observation(value, observed):
                    result = dict(row, observed_at=observed, pending_source=mode, pending_quantity=value,
                                  selected_warehouse=target, stock_basis=product.get('stock_basis', 'company_able'))
                    if mode == 'erp_total':
                        result.update(stock_basis='company_able', warehouse_name='仓库未确认',
                                      able=None, stock=None, purchase=value)
                    elif special:
                        result.update(warehouse_name=target, warehouse_id=product['warehouse_id'],
                                      warehouse_purchase=value, warehouse_stock=depot.get('stock'),
                                      warehouse_able=depot.get('able'), monitor_qty=depot.get('stock'))
                    else:
                        result['purchase'] = value
                    return result
                after = observation(quantity, row['observed_at'])
                before = observation(previous['quantity'], previous['observed_at'])
                event_product = dict(product, warehouse_name='仓库未确认' if mode == 'erp_total' else target)
                # Stock may still be missing. This pending baseline is independent of stock baseline suppression.
                InventoryService._event(self, db, event_product, 'pending_inbound', before, after,
                                        'unassigned_purchase_changed' if mode == 'erp_total' else 'purchase_changed')
            self.put(db, key, dict(current, available=True, checked_at=row['observed_at']))

    def status(self):
        result = super().status()
        with self.db() as db:
            last = self.get(db, 'sql_last_success', 0)
            result['catalog_checked_at'] = self.get(db, 'sql_catalog_updated', 0)
            result['known_warehouses'] = self.get(db, 'sql_known_warehouses', [])
            for product in result['products']:
                pending = self.get(db, 'sql_pending:' + product['sku'], {})
                product['pending_source'] = pending.get('mode')
                product['pending_quantity'] = pending.get('quantity') if pending.get('available') else None
                product['pending_observed_at'] = pending.get('observed_at', 0)
                if pending.get('mode') == 'warehouse':
                    field = 'warehouse_purchase' if product.get('stock_basis') == 'warehouse_stock' else 'purchase'
                    product[field] = product['pending_quantity']
        result['connection_error'] = self._sql_session_error
        if self._sql_session_error:
            result['connection'] = 'error'
        elif not self._sql_session_ready:
            result['connection'] = 'waiting'
        elif last:
            result['connection'] = 'online' if time.time()-last <= result['interval_seconds'] + 60 else 'stale'
        return result

    def accept_snapshot(self, *args, **kwargs):
        result = super().accept_snapshot(*args, **kwargs)
        if result:
            with self.db() as db:
                remaining = set(self.get(db, 'source_baseline_skus', [])) - set(self.get(db, 'last_skus', []))
                self.put(db, 'source_baseline_skus', sorted(remaining))
        return result

    def store_catalog(self, rows, details, scope):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.get(db, 'scope', scope) != scope:
                raise ValueError('数据来源已改变，请核对采集设置。')
            db.execute('DELETE FROM catalog')
            db.executemany('INSERT INTO catalog VALUES (?,?)', [(r['sku'], json.dumps(r, ensure_ascii=False)) for r in rows])
            self.put(db, 'scope', scope)
            self.put(db, 'sql_last_success', now)
            self.put(db, 'sql_catalog_updated', now)
            names = sorted({d['name'] for detail in details.values() for d in detail['depots']})
            names = sorted(set(names) | set(self.get(db, 'sql_known_warehouses', [])))
            self.put(db, 'sql_known_warehouses', names)
            for d in details.values():
                self.put(db, 'warehouse_options:' + d['goods_id'], dict(status='ready', depots=self.warehouse_targets(d['depots'], names),
                    observed_at=now, error='', scope=scope))

    @staticmethod
    def warehouse_targets(depots, names):
        """Allow a watch target in a known warehouse without inventing its stock."""
        present = {d['name'] for d in depots}
        return list(depots) + [dict(id=warehouse_key(name), name=name, stock=None, able=None,
                                   purchase=None, missing_record=True) for name in names if name not in present]

    def accept_warehouses(self, request_id, details, *, scope):
        with self.db() as db:
            names = self.get(db, 'sql_known_warehouses', [])
        if details.get('complete'):
            details = dict(details, depots=self.warehouse_targets(details['depots'], names))
        return super().accept_warehouses(request_id, details, scope=scope)



class SqlWorker:
    """One worker, bounded queries, UI commands never connect on the Tk thread."""
    def __init__(self, service):
        self.service = service
        self.source = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.refresh_requested = False
        self.busy = False
        self.last_call = 0
        self.retry_at = 0
        self.failures = 0
        self.message = '请在采集设置配置 SQL 连接；可选择加密保存密码。'
        self.thread = None

    def configure(self, source):
        with self.lock:
            if self.busy:
                raise ValueError('正在查询，请完成后再修改连接。')
            with self.service.db() as db:
                if self.service.get(db, 'scope', source.scope) != source.scope:
                    raise ValueError('库存数据已绑定另一来源，请核对采集设置。')
            self.source = source
            self.service._sql_session_ready = False
            self.service._sql_session_error = ''
            self.refresh_requested = True
            self.message = '连接设置已应用，等待读取目录。'
        self.service.request_check()

    def refresh_catalog(self):
        with self.lock:
            if self.source is None:
                raise ValueError('请先填写连接设置。')
            self.refresh_requested = True
            self.message = '已安排刷新目录（查询至少间隔 30 秒）。'

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='sql-reader')
        self.thread.start()
        return self

    def run(self):
        while not self.stop_event.wait(.5):
            self.step()

    def step(self, now=None):
        now = time.time() if now is None else now
        s = self.service
        with self.lock:
            if hasattr(s, 'source_mode') and s.source_mode() != 'sql':
                return
            if self.source is None or self.busy or now < max(self.last_call + 30, self.retry_at):
                return
            with s.db() as db:
                due = s.get(db, 'enabled', True) and now >= s.get(db, 'next_check', 0)
                revision = s.get(db, 'config_revision', 0)
            request = s.pending_warehouse_request()
            full = self.refresh_requested
            if not full and not due and request is None:
                return
            source = self.source
            self.busy = True
            self.refresh_requested = False
        try:
            products = [p for p in s.products() if p['enabled']]
            ids = {p['goods_id'] for p in products if p.get('goods_id')} if due or full else set()
            if request:
                ids.add(request['goods_id'])
            if not full and not ids:
                with s.db() as db:
                    s.put(db, 'next_check', now + s.get(db, 'interval_seconds', 7200))
                self.message = '尚无监控商品，请搜索并添加。'
                return
            self.last_call = now
            legacy = getattr(source, 'legacy_text', None)
            if hasattr(source, 'read_bundle'):
                bundle = source.read_bundle(None if full else ids)
                if full and not bundle['catalog']:
                    raise ValueError('库存查询完整目录为空，已保留原目录。')
                rows, details = normalize_bundle(bundle, legacy_text=getattr(source, 'legacy_text', None))
                warehouse_count = len(bundle['warehouses'])
            else:
                raw = source.read(None if full else ids)
                if full and not raw:
                    raise ValueError('完整目录为空，已保留原目录。')
                rows, details = normalize(raw, legacy_text=legacy)
                warehouse_count = len(raw)
            with s.db() as db:
                if revision != s.get(db, 'config_revision', 0):
                    raise ValueError('查询期间监控设置改变，等待按新设置重试。')
            by_id = {r['goods_id']: r for r in rows}
            if hasattr(s, 'adapt_sql_details'):
                details = s.adapt_sql_details(details)
            # SKU is the legacy storage key; never accept another goods_id with that key.
            for p in products:
                if p.get('goods_id') in by_id and by_id[p['goods_id']]['sku'] != p['sku']:
                    raise ValueError('已关注商品编码发生变化，请核对后重新关联。')
            if full:
                s.store_catalog(rows, details, source.scope)
            if request:
                row = by_id.get(request['goods_id'])
                d = details.get(row['sku']) if row else dict(goods_id=request['goods_id'], complete=True, depots=[])
                s.accept_warehouses(request['request_id'], d, scope=source.scope)
            if full or due:
                s.accept_snapshot(uuid.uuid4().hex, rows, scope=source.scope, journals=None,
                    warehouse_details=details, config_revision=revision, preserve_catalog=True)
            with s.db() as db:
                s.put(db, 'sql_last_success', time.time())
            s.connection_update(error='')
            self.failures = 0
            self.retry_at = 0
            missing = [p for p in products if p.get('goods_id') not in by_id] if due or full else []
            self.message = f'查询完成：{len(rows)} 件商品，{warehouse_count} 条仓库记录。'
            if missing:
                self.message += f' {len(missing)} 个关注商品未查到，保留旧值并标为未知。'
            unresolved = []
            for p in products:
                row = by_id.get(p.get('goods_id'))
                if row:
                    target = p.get('warehouse_name') if p.get('stock_basis') == 'warehouse_stock' else '公司大库'
                    depots = details[row['sku']]['depots']
                    if not any(d['name'] == target for d in depots):
                        unresolved.append(p)
            if unresolved:
                self.message += f' {len(unresolved)} 个关注商品缺少所选库房记录，库存待核验。'
            with s.db() as db:
                total_pending = sum(1 for p in products if (state := s.get(db, 'sql_pending:' + p['sku'], {})).get('mode') == 'erp_total' and state.get('available') and (state.get('quantity') or 0) > 0)
            if total_pending:
                self.message += f' {total_pending} 件商品另有 ERP 总待入（仓库未确认），独立监测变化。'
        except Exception as error:
            self.failures += 1
            self.retry_at = now + min(1800, 60 * 2 ** min(self.failures-1, 5))
            message = str(error) if isinstance(error, ValueError) else '读取失败，已保留原数据，请核对连接及字段。'
            s.connection_update(error=message)
            self.message = message + '（失败后退避重试）'
            if full:
                self.refresh_requested = True
        finally:
            with self.lock:
                self.busy = False
