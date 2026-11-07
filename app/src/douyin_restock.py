"""零库存补货提醒：按店铺、链接汇总，仅生成本地 Markdown，不提供发送入口。"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import time
import uuid

from douyin_scope import MACHINE_SCOPE, product_url

CHINA = timezone(timedelta(hours=8))


def stamp(value):
    """机器人文案固定使用北京时间，不受电脑时区影响。"""
    return datetime.fromtimestamp(value, CHINA).strftime('%Y-%m-%d %H:%M:%S') if value else '未核对'


def label(value):
    """商品和配置名称作为普通文字展示，避免换行或 Markdown 符号破坏分组。"""
    return re.sub(r'([\\`*_{}\[\]()#+.!>|])', r'\\\1', ' '.join(str(value or '').split()))


def eligible(row, field, rules=None):
    """缺货、未知、旧套餐来源或没有完整配件证据时，一律不生成补库存提醒。"""
    from douyin_low_stock import is_low
    erp = row.get('erp') or {}
    if (field != 'export_sellable' or row.get('monitor_scope') != MACHINE_SCOPE
            or row.get('quantity') != 0
            or not is_low(row, rules or {'default': 1, 'parts': {}})
            or erp.get('source') != 'maintenance' or erp.get('state') != '配件齐全'
            or erp.get('shortages') or erp.get('unknown') or not erp.get('parts')
            or not product_url(row.get('product_id', ''))):
        return False
    for part in erp['parts']:
        required, available = part.get('required'), part.get('available')
        if (type(required) is not int or required <= 0 or type(available) not in (int, float)
                or not math.isfinite(available) or available < required):
            return False
    return True


def compose(config, rows, *, inventory_at, detected_at=None):
    """每店一份汇总；每个链接只列符合条件的 SKU，保留平台与配件各自的时间。"""
    detected_at = time.time() if detected_at is None else detected_at
    from douyin_low_stock import is_low, threshold
    rules = config.get('low_stock', {'default': 1, 'parts': {}})
    groups = {}
    alerts = []
    excluded = set(config.get('excluded_products', []))
    for row in sorted(rows, key=lambda r: (r['product_id'], r['sku_id'])):
        if row['product_id'] in excluded:
            continue
        if config['field'] == 'export_sellable' and row.get('monitor_scope') == MACHINE_SCOPE and is_low(row, rules):
            alerts.append(dict(product_id=row['product_id'], product_name=row['product_name'],
                sku_id=row['sku_id'], sku_name=row['sku_name'], quantity=row['quantity'],
                threshold=threshold(row, rules), parts_checked=row.get('erp',{}).get('checked',0),
                erp_state=row.get('erp',{}).get('state','待核对')))
        if not eligible(row, config['field'], rules):
            continue
        group = groups.setdefault(row['product_id'], dict(product_id=row['product_id'],
            product_name=row['product_name'], url=product_url(row['product_id']), skus=[]))
        group['skus'].append(dict(sku_id=row['sku_id'], sku_name=row['sku_name'],
                                 quantity=row['quantity'], threshold=threshold(row, rules), parts_checked=row['erp'].get('checked', 0)))
    groups = list(groups.values())
    identities = [(g['product_id'], s['sku_id']) for g in groups for s in g['skus']]
    result = dict(store_id=config['id'], store_name=config['name'], groups=groups,
                  product_count=len(groups), sku_count=len(identities), inventory_at=inventory_at,
                  detected_at=detected_at, status='preview', payload=None, format_version=4, alerts=alerts, low_stock=rules,
                  excluded_count=len(excluded),
                  signature=hashlib.sha256(json.dumps(identities).encode()).hexdigest())
    if not groups:
        return result
    result['payload'] = brief(result, config.get('online_url', ''))
    return result


def brief(preview, url=''):
    """群内只展示汇总和入口；SKU 编号与逐项配置保留在在线表格。"""
    if not preview.get('sku_count'):
        return None
    title = '补库存提醒｜' + preview['store_name']
    part_times = [s['parts_checked'] for g in preview['groups'] for s in g['skus']]
    lines = ['# ' + label(title),
             f"**{preview['product_count']} 个链接 · {preview['sku_count']} 个零库存 SKU 可补库存**",
             '配件齐全，请核对活动占用后处理。',
             f'[查看在线明细并标记处理]({url})' if url else '在线明细暂未配置',
             '库存快照：' + stamp(preview['inventory_at']), '配件核对：' + stamp(min(part_times))]
    return dict(msgtype='markdown', markdown=dict(title=title, text='\n\n'.join(lines)))


def sync_preview(db, config, rows, *, inventory_at, detected_at=None):
    """与库存快照在同一事务更新；只有候选集合变化才追加本地记录，绝不排队发送。"""
    # 排除逻辑统一放在提醒入口，手动核对、首次导出及定时导出均使用同一名单。
    config = dict(config, excluded_products=[r[0] for r in db.execute(
        'SELECT product FROM excluded_products WHERE store=?', (config['id'],))])
    from douyin_low_stock import load
    config['low_stock'] = load(db, config['id'])
    current = compose(config, rows, inventory_at=inventory_at, detected_at=detected_at)
    old = db.execute('SELECT data FROM restock_previews WHERE store=?', (config['id'],)).fetchone()
    prior = json.loads(old[0]) if old else {}
    encoded = json.dumps(current, ensure_ascii=False)
    db.execute('INSERT OR REPLACE INTO restock_previews(store,data) VALUES (?,?)', (config['id'], encoded))
    if current['payload'] and current['signature'] != prior.get('signature'):
        db.execute('INSERT INTO restock_records(id,store,data,created) VALUES (?,?,?,?)',
                   (uuid.uuid4().hex, config['id'], encoded, current['detected_at']))
    return current
