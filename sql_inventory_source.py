"""SQL Server read-only source. No browser state or stored credentials."""
import base64
import hashlib
import json
import math
import re

FIELDS = ('goods_id', '库房', '商品编码', '商品名称', '分库数', '分库可销数', '分库待入')
SELECT = 'SELECT ' + ', '.join('[' + f + ']' for f in FIELDS) + ' FROM [库存].[分库库存]'
CATALOG_FIELDS = ('goods_id', '商品编码', '商品名称', '总数量', '可销数', '待入')
CATALOG_SELECT = 'SELECT ' + ', '.join('[' + f + ']' for f in CATALOG_FIELDS) + ' FROM [库存].[库存查询]'
MAX_ROWS = 100000

# These warehouse names were verified against the user's DBX exports and beta
# cache. Recognition selects a reversible transport correction, never fuzzy IDs.
KNOWN_WAREHOUSES = ('公司大库', '样品库', '京仓', '电商礼品库', '二手分库', '固定资产库',
                    '机箱售后', '良品库', '损耗库', '稀缺货源', '新零售礼品库', '整机预装库')
LEGACY_MARKERS = {name.encode('gb18030').decode('latin1') for name in KNOWN_WAREHOUSES}


def detect_legacy_text(rows):
    return any(row.get('库房') in LEGACY_MARKERS for row in rows)


def decode_legacy_text(value):
    """Recover verified GBK/GB18030 bytes transported as Latin-1 Unicode."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('数据库文本字段类型无效。')
    if value.isascii():
        return value
    try:
        raw = value.encode('latin1')
    except UnicodeEncodeError:
        return value  # Already Unicode (e.g. NVARCHAR); do not re-decode it.
    try:
        fixed = raw.decode('gb18030')
    except UnicodeDecodeError:
        raise ValueError('数据库文本编码不一致，已保留原数据；请核对数据库字符集。') from None
    if fixed.encode('gb18030') != raw:
        raise ValueError('数据库文本无法无损还原，已停止本轮更新。')
    return fixed


def warehouse_key(name):
    return 'sqlname:' + base64.urlsafe_b64encode(name.encode('utf-8')).decode('ascii')


def valid_warehouse_identity(ident, name):
    return bool(re.fullmatch(r'[1-9][0-9]{0,19}', ident)) or ident == warehouse_key(name)


def numeric(value, field, null_zero=False):
    if value is None:
        return 0 if null_zero else None
    if isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise ValueError(f'{field}格式无效，未覆盖原数据。')
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'{field}不是有效数字。') from None
    if not math.isfinite(value):
        raise ValueError(f'{field}不是有限数字。')
    return int(value) if value.is_integer() else value


def normalize(raw, legacy_text=None):
    """Return company catalog plus all-warehouse details; names are local keys."""
    legacy_text = detect_legacy_text(raw) if legacy_text is None else legacy_text
    groups, keys, skus = {}, set(), {}
    for original in raw:
        row = dict(original)
        if legacy_text:
            for field in ('库房', '商品编码', '商品名称'):
                if field in row:
                    row[field] = decode_legacy_text(row[field])
        if not all(k in row for k in FIELDS):
            raise ValueError('数据库缺少必要字段，本轮不更新。')
        gid = str(row['goods_id'])
        name = str(row['商品名称'] or '').strip()
        sku = str(row['商品编码'] or '').strip()
        warehouse = str(row['库房'] or '').strip()
        if not re.fullmatch(r'[1-9][0-9]{0,19}', gid) or not name or not sku or sku == '0' or not warehouse or len(warehouse) > 100:
            raise ValueError('商品或库房身份不完整。')
        key = (gid, warehouse)
        if key in keys:
            raise ValueError('同一商品和库房出现重复记录，不能自动合并。')
        keys.add(key)
        if sku in skus and skus[sku] != gid:
            raise ValueError('同一商品编码对应多个 goods_id，请先核对。')
        skus[sku] = gid
        group = groups.setdefault(gid, dict(sku=sku, goods_id=gid, name=name, depots=[]))
        if group['sku'] != sku:
            raise ValueError('同一 goods_id 出现不同商品编码。')
        group['depots'].append(dict(id=warehouse_key(warehouse), name=warehouse,
            stock=numeric(row['分库数'], '分库数'), able=numeric(row['分库可销数'], '分库可销数'),
            purchase=numeric(row['分库待入'], '分库待入', null_zero=True)))
    catalog, details = [], {}
    for group in groups.values():
        depots = group.pop('depots')
        main = next((d for d in depots if d['name'] == '公司大库'), {})
        row = dict(group, able=main.get('able'), stock=main.get('stock'), purchase=main.get('purchase'))
        if row['able'] is None or row['stock'] is None:
            row.update(able=None, stock_unknown=True)
        catalog.append(row)
        details[group['sku']] = dict(goods_id=group['goods_id'], complete=True, depots=depots)
    return catalog, details


def normalize_bundle(bundle, legacy_text=None):
    """Full directory is independent from the sparse per-warehouse relation."""
    raw = bundle['warehouses']
    legacy_text = detect_legacy_text(raw) if legacy_text is None else legacy_text
    warehouse_rows, details = normalize(raw, legacy_text=legacy_text)
    by_id = {r['goods_id']: r for r in warehouse_rows}
    codes = {r['sku']: r['goods_id'] for r in warehouse_rows}
    seen, result = set(), []
    for original in bundle['catalog']:
        row = dict(original)
        if not all(f in row for f in CATALOG_FIELDS):
            raise ValueError('库存查询目录缺少必要字段，保留原目录。')
        gid = str(row['goods_id'])
        if legacy_text:
            for field in ('商品编码', '商品名称'):
                row[field] = decode_legacy_text(row[field])
        sku, name = str(row['商品编码'] or '').strip(), str(row['商品名称'] or '').strip()
        if not re.fullmatch(r'[1-9][0-9]{0,19}', gid) or gid in seen or not sku or sku == '0' or not name:
            raise ValueError('完整目录存在缺失或重复商品身份。')
        if sku in codes and codes[sku] != gid:
            raise ValueError('完整目录商品编码不唯一，请核对。')
        if gid in by_id and by_id[gid]['sku'] != sku:
            raise ValueError('两张库存表的商品编号与编码不一致，保留原数据。')
        seen.add(gid); codes[sku] = gid
        merged = dict(by_id.get(gid) or dict(goods_id=gid, sku=sku, able=None, stock=None,
                                            purchase=None, stock_unknown=True))
        merged.update(name=name, catalog_stock=numeric(row['总数量'], '总数量'),
                      catalog_able=numeric(row['可销数'], '总可销数'),
                      catalog_purchase=numeric(row['待入'], '总待入'),
                      catalog_source='库存查询', warehouse_missing=gid not in by_id)
        # No warehouse row is NOT a zero observation. Totals only serve directory display.
        details.setdefault(sku, dict(goods_id=gid, complete=True, depots=[]))
        result.append(merged)
    # Keep newly appearing goods which have not yet reached the directory relation.
    result.extend(r for r in warehouse_rows if r['goods_id'] not in seen)
    return result, details


class SqlSource:
    def __init__(self, settings, password, connector=None):
        self.settings = dict(settings)
        self.password = password
        self.connector = connector
        self.legacy_text = None
        if not all(str(settings.get(k, '')).strip() for k in ('server', 'database', 'user')) or not password:
            raise ValueError('请填写服务器、数据库、账号和密码。')
        if not 1 <= int(settings.get('port', 1433)) <= 65535:
            raise ValueError('端口无效。')
        self.scope = 'sql:' + hashlib.sha256(json.dumps(
            [settings['server'], int(settings.get('port', 1433)), settings['database'], settings['user']],
            ensure_ascii=False).encode()).hexdigest()

    def read(self, goods_ids=None):
        return self._read_queries([SELECT], goods_ids)[0]

    def read_bundle(self, goods_ids=None):
        warehouses, catalog = self._read_queries([SELECT, CATALOG_SELECT], goods_ids)
        return dict(warehouses=warehouses, catalog=catalog)

    def _read_queries(self, queries, goods_ids=None):
        if goods_ids is not None:
            goods_ids = sorted(set(str(x) for x in goods_ids))
            if not goods_ids:
                return [[] for _ in queries]
            if len(goods_ids) > 1000 or any(not re.fullmatch(r'[1-9][0-9]{0,19}', g) for g in goods_ids):
                raise ValueError('本次商品编号无效或超过测试版单批 1000 件上限。')
        connector = self.connector
        if connector is None:
            import pymssql
            connector = pymssql.connect
        connection = None
        try:
            connection = connector(server=self.settings['server'], port=str(self.settings.get('port', 1433)),
                database=self.settings['database'], user=self.settings['user'], password=self.password,
                login_timeout=10, timeout=30, charset='UTF-8', as_dict=True, autocommit=True,
                appname='InventoryMonitorReadonly', tds_version='7.4')
            cursor = connection.cursor()
            results = []
            for query in queries:
                params = None
                if goods_ids is not None:
                    query += ' WHERE [goods_id] IN (' + ','.join(['%s'] * len(goods_ids)) + ')'
                    params = tuple(int(g) for g in goods_ids)
                if params is None:
                    cursor.execute(query)
                else:
                    cursor.execute(query, params)
                rows = []
                while True:
                    chunk = cursor.fetchmany(1000)
                    if not chunk:
                        break
                    rows.extend(chunk)
                    if len(rows) > MAX_ROWS:
                        raise ValueError('结果超过测试版 100000 行上限，已拒收截断数据。')
                if detect_legacy_text(rows):
                    self.legacy_text = True
                results.append(rows)
            return results
        except ValueError:
            raise
        except Exception:
            # Driver exceptions can contain login parameters. Never persist/render them.
            raise ValueError('数据库连接或查询失败。请核对网络、账号权限和数据库名；原数据保留。') from None
        finally:
            if connection is not None:
                connection.close()
