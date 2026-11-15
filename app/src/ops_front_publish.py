"""把前台库存同步已回读的行登记为群查询批次。"""
from datetime import datetime, timezone
import json
from pathlib import Path

from douyin_online import DwsTable, plain


def publish(root, shop, config, receipt, *, verified_rows=(), client_factory=DwsTable):
    # 只有显式绑定的独立批次表才写入；原在线明细由原同步维护。
    source_file = Path(root) / 'ops-command-sources.json'
    if not source_file.is_file():
        return
    source = json.loads(source_file.read_text(encoding='utf-8-sig'))
    target = source.get('front_report_writer')
    scope = str(shop.get('maintenance_shop_id') or '')
    if not target or not scope or scope not in target.get('shop_ids', ()):
        return
    if config['profile'] != target['profile'] or (config['base_id'], config['table_id']) == (target['base_id'], target['table_id']):
        raise ValueError('前台库存批次绑定的组织或表格身份无效')
    writer = client_factory(target)
    at = datetime.fromtimestamp(receipt['at'], timezone.utc).isoformat()
    batch = receipt['fingerprint']
    rows = []
    if receipt.get('ok') is True:
        original = verified_rows
        by_key = {}
        fields = config['fields']
        for entry in original:
            row = entry['cells']
            ident = plain(row.get(fields['关联键']))
            if not isinstance(ident, str) or not ident.startswith(str(shop['id']) + ':'):
                continue
            if ident in by_key:
                raise ValueError('前台库存表存在重复业务键')
            spu, sku = (plain(row.get(fields[k])) for k in ('商品ID', 'SKU ID'))
            if not spu or not sku:
                continue
            flag = plain(row.get(fields['补库存标记']))
            if flag not in ('待补库存', '低库存', '待核对', '待人工核对', 'SKU 待核对', 'SKU待核对'):
                continue
            stock = plain(row.get(fields['平台库存']))
            if isinstance(stock, str) and stock.isascii() and stock.isdecimal():
                stock = int(stock)
            by_key[ident] = dict(shop_id=scope, spu=str(spu), sku_id=str(sku), active=True,
                                 platform_stock=stock if type(stock) is int and stock >= 0 else None,
                                 **{key: plain(row.get(fields[title])) for key, title in (
                                     ('product_name','商品名称'), ('config_name','配置名称'), ('parts_state','配件检查'),
                                     ('replenish_state','补库存标记'), ('handling_state','处理状态'),
                                     ('snapshot_at','平台快照时间'), ('parts_checked_at','配件核对时间'))})
        rows = list(by_key.values())
    fields = target['fields']

    def payload_text(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))

    desired = {}
    parts, current = [], []
    if len(payload_text(rows)) > 7000:
        # Windows 命令行与文本字段有长度限制；先写明细分片，全部回读后再登记已核实批次。
        for row in rows:
            if current and len(payload_text(current + [row])) > 7000:
                parts.append(current)
                current = []
            current.append(row)
        if current:
            parts.append(current)
        rows = []
    segments = [f'{batch}:part:{idx}' for idx in range(len(parts))]
    for ident, items in zip(segments, parts):
        desired[ident] = ('fragment', dict(items=items, parent=batch))
    desired[batch] = ('verified' if receipt.get('ok') else 'unknown' if receipt.get('uncertain') else 'failed',
                      dict(complete=receipt.get('ok') is True, items=rows, fragments=segments,
                           table_identity=[config['base_id'], config['table_id']]))

    def read():
        found = {}
        for row in writer.query():
            cells = row['cells']
            ident = plain(cells.get(fields['batch']))
            if plain(cells.get(fields['scope'])) != scope or ident not in desired:
                continue
            if ident in found:
                raise ValueError('前台库存批次存在重复业务键')
            found[ident] = row
        return found

    found = read()
    for ident, (state, payload) in desired.items():
        values = dict(scope=scope, batch=ident, checked_at=at, sync_state=state, payload=payload_text(payload))
        wanted = {fields[k]: v for k, v in values.items()}
        def matches(row):
            return row and all(plain(row['cells'].get(k)) == v for k, v in wanted.items())
        prior = found.get(ident)
        if matches(prior):
            continue
        if prior:
            writer.write(['record','update'], [dict(recordId=prior['recordId'], cells=wanted)])
        else:
            writer.write(['record','create'], [dict(cells=wanted)])
        if not matches(read().get(ident)):
            raise ValueError('前台库存批次回读未确认')
