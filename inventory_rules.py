"""Pure inventory policy. No network, credentials, or ERP side effects."""
import math
import re
from sql_inventory_source import valid_warehouse_identity

ALERTS = dict(inbound=True, pending_inbound=True, low=True, oversold=True, negative_stock=True, recovery=True)
RISK_TEXT = {'normal': '正常', 'low': '低库存', 'oversold': '超售', 'negative_stock': '负库存', 'unknown': '待检查', 'disabled': '已停用'}


def default_threshold(category, name=''):
    if category == '显卡':
        # Explicit current family, not lexicographic/numeric performance guessing.
        match = re.search(r'(?i)RTX\s*(50[789]0)(?:\b|TI|SUPER|显卡)', name)
        return 5 if match else 20
    return {'主板': 10, '内存': 10, 'CPU': 20}.get(category)


def quantity(value):
    if isinstance(value, bool) or value is None or value == '':
        raise ValueError('库存数量无效，请重新检查 ERP 数据。')
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError('库存数量无效，请重新检查 ERP 数据。') from None
    if not math.isfinite(n):
        raise ValueError('库存数量必须为有限数字。')
    return int(n) if n.is_integer() else n


def classify(able, threshold):
    return 'oversold' if able < 0 else ('low' if able <= threshold else 'normal')


def validate_product(data):
    sku = str(data.get('sku', '')).strip()
    name = str(data.get('name', '')).strip()
    category = str(data.get('category', '其他'))
    if not sku or sku in ('0', '-', '—') or len(sku) > 160 or not name or len(name) > 400:
        raise ValueError('请填写真实 ERP 商品编码和产品名称。')
    threshold = data.get('threshold', default_threshold(category, name))
    threshold = quantity(threshold)
    if threshold < 0 or int(threshold) != threshold or threshold > 100000000:
        raise ValueError('预警数量必须是非负整数。')
    basis = data.get('stock_basis', 'company_able')
    if basis not in ('company_able', 'warehouse_stock'):
        raise ValueError('未知预警依据。')
    depot_id, depot_name = '', ''
    if basis == 'warehouse_stock':
        depot_id, depot_name = str(data.get('warehouse_id', '')).strip(), str(data.get('warehouse_name', '')).strip()
        if not valid_warehouse_identity(depot_id, depot_name) or not depot_name or len(depot_name) > 100:
            raise ValueError('请刷新分库列表并选择一个真实分库。')
    return dict(sku=sku, goods_id=str(data.get('goods_id', '')).strip(), name=name, category=category, threshold=int(threshold),
                stock_basis=basis, warehouse_id=depot_id, warehouse_name=depot_name,
                keywords=str(data.get('keywords', '')).strip()[:1000], enabled=bool(data.get('enabled', True)),
                alerts={k: bool(data.get('alerts', {}).get(k, v)) for k, v in ALERTS.items()})
