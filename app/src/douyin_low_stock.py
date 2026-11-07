"""抖店 SKU 低库存规则；只使用精确 ERP 配件编号，不根据名称猜配件。"""
import json


def validate(value):
    """校验默认阈值和店铺内配件例外；空规则默认预警数量为 1。"""
    default = value.get('default', 1)
    rules = value.get('parts', {})
    if type(default) is not int or not 0 <= default <= 1000000 or not isinstance(rules, dict):
        raise ValueError('预警数量须为 0～1000000 的整数')
    clean = {}
    for gid, threshold in rules.items():
        if not isinstance(gid, str) or not gid.isascii() or not gid.isdigit() or int(gid) <= 0:
            raise ValueError('配件编号必须是 ERP goods_id 正整数')
        if threshold is not None and (type(threshold) is not int or not 0 <= threshold <= 1000000):
            raise ValueError('配件预警数量须为整数；不预警使用空值')
        clean[str(int(gid))] = threshold
    return dict(default=default, parts=clean)


def load(db, ident):
    row = db.execute('SELECT value FROM preferences WHERE key=?', ('low_stock:' + ident,)).fetchone()
    return validate(json.loads(row[0]) if row else {})


def threshold(row, rules):
    """多配件例外中不预警优先，否则取较高的例外阈值；未匹配规则使用默认值。"""
    gids = {str(p.get('goods_id', '')) for p in row.get('erp', {}).get('parts', [])}
    matched = [value for gid, value in rules.get('parts', {}).items() if gid in gids]
    if None in matched:
        return None
    return max(matched) if matched else rules.get('default', 1)


def is_low(row, rules):
    limit = threshold(row, rules)
    quantity = row.get('quantity')
    return limit is not None and type(quantity) is int and 0 <= quantity <= limit
