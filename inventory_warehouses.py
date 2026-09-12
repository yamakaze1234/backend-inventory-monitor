"""Per-product warehouse selection and correlated, read-only detail lookups."""
import json
import re
import time
import uuid

LOOKUP_TTL = 300


class ConfigurationChanged(ValueError):
    """A snapshot belongs to an older product configuration."""


def source_key(product):
    return (product.get('stock_basis', 'company_able'),
            product.get('warehouse_id', '') if product.get('stock_basis') == 'warehouse_stock' else '')


def monitor_quantity(row):
    return row.get('monitor_qty', row.get('able'))


def inventory_risk(row, threshold):
    from inventory_rules import classify
    value = monitor_quantity(row)
    if value is None:
        return 'unknown'
    risk = classify(value, threshold)
    return 'negative_stock' if risk == 'oversold' and row.get('stock_basis') == 'warehouse_stock' else risk


def normalize_details(details, goods_id):
    from inventory_rules import quantity
    if (not isinstance(details, dict) or details.get('complete') is not True
            or str(details.get('goods_id', '')) != str(goods_id)):
        raise ValueError('分库数据未完成或商品编号不匹配。')
    raw = details.get('depots')
    if not isinstance(raw, list) or len(raw) > 500:
        raise ValueError('分库列表无效。')
    depots, seen = [], set()
    for depot in raw:
        if not isinstance(depot, dict):
            raise ValueError('分库数据无效。')
        ident, name = str(depot.get('id', '')), str(depot.get('name', '')).strip()
        if not re.fullmatch(r'[1-9][0-9]{0,19}', ident) or ident in seen or not name or len(name) > 100:
            raise ValueError('分库身份缺失或重复。')
        seen.add(ident)
        depots.append(dict(id=ident, name=name, **{
            key: None if depot.get(key) is None else quantity(depot[key]) for key in ('stock', 'able', 'purchase')}))
    return depots


def select_inventory(product, row, details):
    """Return a usable observation without substituting warehouse stock for able."""
    current = dict(row, stock_basis=product.get('stock_basis', 'company_able'),
                   warehouse_id=product.get('warehouse_id', ''), warehouse_name=product.get('warehouse_name', ''))
    if current['stock_basis'] == 'company_able':
        if row.get('stock_unknown') or row.get('able') is None:
            return None
        current['monitor_qty'] = row['able']
        return current
    expected_id = product.get('goods_id') or row.get('goods_id')
    if not expected_id or str(row.get('goods_id', '')) != str(expected_id):
        return None
    try:
        depots = normalize_details(details, expected_id)
    except (ValueError, TypeError):
        return None
    selected = next((d for d in depots if d['id'] == current['warehouse_id']
                     and d['name'] == current['warehouse_name']), None)
    if selected is None or selected['stock'] is None:
        return None
    current.update(monitor_qty=selected['stock'], warehouse_stock=selected['stock'],
                   warehouse_able=selected['able'], warehouse_purchase=selected['purchase'])
    return current


class WarehouseLookups:
    def request_warehouses(self, sku, goods_id):
        sku, goods_id = str(sku).strip(), str(goods_id).strip()
        if not re.fullmatch(r'[1-9][0-9]{0,19}', goods_id):
            raise ValueError('请先搜索并选择有真实 ERP 编号的产品。')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT data FROM catalog WHERE sku=? UNION ALL SELECT data FROM products WHERE sku=?', (sku, sku)).fetchall()
            identities = {str(json.loads(r[0]).get('goods_id')) for r in rows if json.loads(r[0]).get('goods_id')}
            if identities != {goods_id}:
                raise ValueError('ERP 编号与产品不匹配，请重新搜索选择。')
            now = time.time()
            pending = [r for r in self.get(db, 'warehouse_requests', []) if now-r['created_at'] < 180 and r['goods_id'] != goods_id]
            if len(pending) >= 32:
                raise ValueError('分库查询较多，请等待当前查询完成。')
            request = dict(request_id=uuid.uuid4().hex, sku=sku, goods_id=goods_id, created_at=now)
            self.put(db, 'warehouse_requests', pending + [request])
            self.put(db, 'warehouse_options:' + goods_id, dict(status='pending', depots=[], observed_at=0,
                     requested_at=now, request_id=request['request_id'], error=''))
            return request

    def warehouse_options(self, goods_id):
        with self.db() as db:
            options = self.get(db, 'warehouse_options:' + str(goods_id), dict(status='missing', depots=[], observed_at=0, error=''))
        if options['status'] == 'pending' and time.time()-options.get('requested_at', 0) > 180:
            return dict(options, status='error', error='分库查询超时，请确认 ERP 采集脚本已更新并在线后重试。')
        if options['status'] == 'ready' and time.time()-options['observed_at'] > LOOKUP_TTL:
            return dict(options, status='error', error='分库列表已过期，请刷新后选择。')
        return options

    def pending_warehouse_request(self):
        with self.db() as db:
            pending = [r for r in self.get(db, 'warehouse_requests', []) if time.time()-r['created_at'] < 180]
        return pending[0] if pending else None

    def accept_warehouses(self, request_id, details, *, scope):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            pending = self.get(db, 'warehouse_requests', [])
            request = next((r for r in pending if r['request_id'] == request_id and time.time()-r['created_at'] < 180), None)
            if request is None or not scope or self.get(db, 'scope', scope) != scope:
                raise ValueError('分库查询已过期或账号不匹配。')
            if not isinstance(details, dict) or str(details.get('goods_id', '')) != request['goods_id']:
                raise ValueError('分库查询商品编号不匹配。')
            if details.get('complete') is not True:
                options = dict(status='error', depots=[], observed_at=0, error='分库查询失败，请刷新重试。')
            else:
                options = dict(status='ready', depots=normalize_details(details, request['goods_id']),
                               observed_at=time.time(), error='', scope=scope)
            self.put(db, 'warehouse_options:' + request['goods_id'], options)
            self.put(db, 'warehouse_requests', [r for r in pending if r['request_id'] != request_id])
            self.put(db, 'scope', scope)
            return options

    def validate_warehouse_selection(self, db, product, previous):
        if product['stock_basis'] != 'warehouse_stock':
            return
        if previous and source_key(product) == source_key(previous) and product['warehouse_name'] == previous.get('warehouse_name'):
            return
        options = self.get(db, 'warehouse_options:' + product['goods_id'], {})
        if (options.get('status') != 'ready' or not 0 <= time.time()-options.get('observed_at', 0) <= LOOKUP_TTL
                or options.get('scope') != self.get(db, 'scope')):
            raise ValueError('请先刷新该产品的分库列表，再选择真实分库。')
        if not any(d['id'] == product['warehouse_id'] and d['name'] == product['warehouse_name'] for d in options['depots']):
            raise ValueError('所选分库不在该产品的已核验分库列表中。')
