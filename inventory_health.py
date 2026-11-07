"""按商品隔离 SQL 数据，并记录每轮实际覆盖范围；不连接外部服务。"""
import re
from collections import defaultdict

from sql_inventory_source import CATALOG_FIELDS, FIELDS, detect_legacy_text, normalize_bundle
from inventory_warehouses import select_inventory


def normalize_isolated(bundle, *, legacy_text=None, issues=None):
    """身份明确的坏商品单独隔离；表结构或身份缺失仍拒绝整轮。"""
    issues = {} if issues is None else issues
    if legacy_text is None:
        legacy_text = detect_legacy_text(bundle.get('warehouses', []))
    groups = defaultdict(lambda: {'catalog': [], 'warehouses': []})
    for table, fields in (('catalog', CATALOG_FIELDS), ('warehouses', FIELDS)):
        for row in bundle.get(table, []):
            gid = str(row.get('goods_id', ''))
            if not all(field in row for field in fields):
                raise ValueError('库存数据缺少必要字段，本轮未采用。')
            if not re.fullmatch(r'[1-9][0-9]{0,19}', gid):
                raise ValueError('商品身份缺失，无法确定隔离对象，本轮未采用。')
            groups[gid][table].append(row)
    rows, details, owners = [], {}, defaultdict(set)
    for gid, group in groups.items():
        try:
            normalized, depots = normalize_bundle(group, legacy_text=legacy_text)
        except ValueError as error:
            issues[gid] = dict(goods_id=gid, reason=str(error))
            continue
        rows.extend(normalized)
        details.update(depots)
        for row in normalized:
            owners[row['sku']].add(gid)
    # 编码碰撞的双方都隔离，不能让后出现的商品覆盖先出现的商品。
    for gids in owners.values():
        if len(gids) > 1:
            for gid in gids:
                issues[gid] = dict(goods_id=gid, reason='同一商品编码对应多个 goods_id，请先核对。')
    rows = [r for r in rows if r['goods_id'] not in issues]
    valid = {r['sku'] for r in rows}
    return rows, {sku: d for sku, d in details.items() if sku in valid}


def isolate_watch_identity(rows, details, products, issues):
    """关注商品绑定保持不变：编码更名与编码被另一商品复用都只隔离相关商品。"""
    by_id = {r['goods_id']: r for r in rows}
    by_sku = {r['sku']: r for r in rows}
    for product in products:
        gid, sku = product.get('goods_id'), product['sku']
        row = by_id.get(gid)
        collision = by_sku.get(sku)
        if row and row['sku'] != sku:
            issues[gid] = dict(goods_id=gid, reason='已关注商品编码发生变化，请核对后重新关联。')
        if collision and gid and collision['goods_id'] != gid:
            for affected in (gid, collision['goods_id']):
                issues[affected] = dict(goods_id=affected, reason='关注编码被其他商品使用，请核对后重新关联。')
    rows = [r for r in rows if r['goods_id'] not in issues]
    return rows, {r['sku']: details[r['sku']] for r in rows if r['sku'] in details}


def build_coverage(products, rows, details, issues, revision, excluded=()):
    """分类互斥且数量守恒；仅统计启用且未排除的关注商品。"""
    by_id = {r['goods_id']: r for r in rows}
    counts = dict(success=0, isolated=0, missing=0, warehouse_missing=0)
    items = []
    for product in products:
        gid = product.get('goods_id')
        if not product['enabled'] or gid in excluded:
            continue
        row = by_id.get(gid)
        reason = ''
        if gid in issues:
            state, reason = 'isolated', issues[gid]['reason']
        elif row is None:
            state, reason = 'missing', '本轮未返回该商品'
        elif select_inventory(product, row, details.get(row['sku'])) is None:
            state, reason = 'warehouse_missing', '所选库房记录缺失或监控数量未知'
        else:
            state = 'success'
        counts[state] += 1
        items.append(dict(sku=product['sku'], goods_id=gid, name=product['name'], state=state,
                          warehouse=product.get('warehouse_name') or '公司大库', reason=reason))
    return dict(expected=len(items), counts=counts, items=items, revision=revision,
                excluded=sum(p['enabled'] and p.get('goods_id') in excluded for p in products))
