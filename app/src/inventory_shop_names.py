"""用当前工作区已配置的店名显示 ERP 店铺身份；编号只用于业务关联。"""
from collections import defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3

from order_bot_config import load


def shop_names(root):
    """先用前台库存的维护表绑定，再补充本部门订单设置及已移除店铺。"""
    root = Path(root)
    active, archived = defaultdict(set), defaultdict(set)
    path = root / 'douyin_inventory' / 'inventory.sqlite3'
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                for ident, name, deleted in db.execute(
                    "SELECT maintenance_shop_id, name, deleted FROM stores WHERE maintenance_shop_id<>''"
                ):
                    if name.strip():
                        (archived if deleted else active)[str(ident)].add(name.strip())
        except sqlite3.Error:
            # 新工作区、旧库或短暂锁定时，仍可使用本部门订单设置。
            pass
    try:
        configured = load(root).get('shops') or []
    except (ValueError, AttributeError, TypeError):
        configured = []
    names = {ident: next(iter(labels)) for ident, labels in archived.items() if len(labels) == 1}
    for row in configured:
        if isinstance(row, dict) and row.get('id') and isinstance(row.get('name'), str) and row['name'].strip():
            names[str(row['id'])] = row['name'].strip()
    for ident, labels in active.items():
        # 同一编号绑定多个不同名称时，以明确配置的部门名称为准。
        if len(labels) == 1:
            names[ident] = next(iter(labels))
    return names


def shop_label(names, ident):
    return names.get(str(ident), '未配置店铺名称') if ident else '—'


def named_shop_rows(rows, names):
    # 仅为展示补充字段，不改历史记录或用于绑定核对的原编号。
    return [dict(row, shop_name=shop_label(names, row.get('shop_id'))) for row in rows]
