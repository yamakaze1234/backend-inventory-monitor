"""订单回读及钉钉 AI 表格只读投影。"""
from datetime import datetime, timedelta, timezone
import json
import math
import re
from urllib.parse import urlsplit

from ops_command_api import CommandError, bad, denied

CN = timezone(timedelta(hours=8))
ROLES = ('客服', '操作', '库房打包', '系统装机')
NUMBER_TYPES = dict(web_order_no='网店单号', erp_no='NO单号', outbound_no='出库单号', tracking_no='物流单号')
COUNTS = ('pending_scanned', 'overdue', 'due_within_48h', 'deadline_unknown', 'shortage_goods', 'inventory_unknown_goods')


def stamp(value):
    # 时间未知时保留记录；无时区的已登记时间按北京时间解释。
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, CN).timestamp()
        if isinstance(value, str):
            dt = datetime.fromisoformat(value)
            return (dt if dt.tzinfo else dt.replace(tzinfo=CN)).timestamp()
    except (ValueError, OSError, OverflowError):
        pass
    return None


def unavailable(message='数据源暂不可读'):
    return CommandError(503, 'SOURCE_UNAVAILABLE', message)


def order_status(read, args, access):
    number, kind = args['number'], args.get('number_type', 'web_order_no')
    upper = number.upper()
    if ((kind == 'erp_no') != upper.startswith('NO-') or (kind == 'outbound_no') != upper.startswith('CK-')
            or upper in ('NO-', 'CK-') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,149}', number)):
        raise bad('请提供完整单号，并核对查询类型')
    shop = args.get('shop_id')
    allowed = {shop} if shop else access.shops
    if not allowed:
        raise denied()
    try:
        result = read(NUMBER_TYPES[kind], number, sorted(allowed))
    except Exception:
        raise unavailable('本次未能完整读取订单记录') from None
    if not isinstance(result, dict) or result.get('status') not in ('ok', 'ambiguous', 'unverified'):
        raise unavailable('本次未能完整读取订单记录')
    if result['status'] == 'unverified':
        raise denied()
    orders = result.get('orders')
    if not isinstance(orders, list) or any(not isinstance(o, dict) or not o.get('erp_no') or o.get('shop_id') not in allowed for o in orders):
        raise denied()
    if result['status'] == 'ambiguous':
        raise CommandError(409, 'AMBIGUOUS_TARGET', '请补充店铺或提供带完整后缀的网店单号')
    if not orders:
        raise CommandError(404, 'NOT_FOUND', '在已查询范围内未找到关联订单')
    keys = {(o['shop_id'], o['erp_no']) for o in orders}
    if kind == 'web_order_no' and (len(keys) != 1 or any(number not in o.get('web_order_numbers', []) for o in orders)):
        raise CommandError(409, 'AMBIGUOUS_TARGET', '未能唯一核实完整网店单号')
    if len(keys) != len(orders):
        raise unavailable('订单关联结果不完整')
    events = []
    for order in orders:
        for event in order['events']:
            events.append(dict(step=event['step'], operator=event.get('operator') if 'operator' in access.fields else None,
                               occurred_at=stamp(event.get('occurred_at')), reference_number=event.get('reference_number')))
    events.sort(key=lambda e: (e['occurred_at'] is None, e['occurred_at'] or 0))
    newest = max((e['occurred_at'] for e in events if e['occurred_at'] is not None), default=None)
    return dict(number=number, number_type=kind, order_count=len(keys), shop_ids=sorted({k[0] for k in keys}),
                outcome='ready' if events else 'no_records', events=events,
                latest_events=[e for e in events if newest is not None and e['occurred_at'] == newest])


def safe_link(url):
    if isinstance(url, str):
        parts = urlsplit(url)
        if parts.scheme == 'https' and parts.hostname and (parts.hostname == 'dingtalk.com' or parts.hostname.endswith('.dingtalk.com')) and not parts.username:
            return url
    return None


def plain(cell):
    # 兼容已核对的文本字段，拒绝把任意富对象当作字符串。
    if isinstance(cell, list) and all(isinstance(x, dict) and isinstance(x.get('text'), str) for x in cell):
        return ''.join(x['text'] for x in cell)
    return cell


class ReportTables:
    """每个绑定按稳定字段 ID 读取批次回执；查询不触发同步。"""
    def __init__(self, bindings=None, reader=None):
        self.bindings = bindings or {}
        self.reader = reader or self._read

    @staticmethod
    def _read(binding, scope):
        from douyin_online import DwsTable
        fields = binding['fields']
        client = DwsTable(binding)
        result, ids, cursors, cursor = [], set(), set(), ''
        filters = dict(operator='and', operands=[dict(operator='eq', operands=[fields['scope'], scope])])
        for _ in range(50):
            cmd = ['record', 'query', '--limit', '100', '--field-ids', ','.join(fields.values()),
                   '--filters', json.dumps(filters, ensure_ascii=False)]
            try:
                value = client.call(cmd + (['--cursor', cursor] if cursor else []))
                records = value['records']
                if not isinstance(records, list):
                    raise ValueError
                for row in records:
                    rid = row['recordId']
                    if rid in ids:
                        return result, False
                    ids.add(rid)
                    cells = row['cells']
                    result.append({name: plain(cells.get(field)) for name, field in fields.items()})
                if value.get('hasMore') is False:
                    return result, True
                cursor = value.get('nextCursor')
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    break
                cursors.add(cursor)
            except Exception:
                if not result:
                    raise
                break
        return result, False

    def _batch(self, name, shops):
        binding = self.bindings.get(name)
        if not binding:
            return dict(source=name, state='not_bound')
        scope = ','.join(sorted(shops))
        if not set(shops) <= set(binding['shop_ids']):
            return dict(source=name, state='forbidden')
        try:
            rows, complete = self.reader(binding, scope)
            # 范围和批次必须双重核对；分页中断不能宣称全量。
            eligible = sorted((r for r in rows if r.get('scope') == scope and stamp(r.get('checked_at')) is not None
                               and r.get('sync_state') != 'fragment'),
                              key=lambda r: (stamp(r['checked_at']), str(r.get('batch'))), reverse=True)
            valid = [r for r in eligible if r.get('sync_state') == 'verified' and isinstance(r.get('batch'), str) and r['batch']]
            if not valid:
                state = ('partial' if not complete else eligible[0].get('sync_state') if eligible else 'no_report')
                return dict(source=name, state=state if state in ('partial', 'unknown', 'failed', 'no_report') else 'unknown')
            selected = valid[0]
            if sum(r['batch'] == selected['batch'] for r in valid) != 1:
                return dict(source=name, state='unknown')
            payload = json.loads(selected['payload'])
            if not isinstance(payload, dict):
                raise ValueError
            state = 'verified' if complete and selected == eligible[0] and payload.get('complete') is True else 'partial'
            fragments = payload.get('fragments', [])
            if name == 'front_stock' and fragments:
                items = []
                if not isinstance(fragments, list) or len(fragments) > 500 or len(set(fragments)) != len(fragments):
                    raise ValueError('前台库存批次分片无效')
                for ident in fragments:
                    found = [r for r in rows if r.get('scope') == scope and r.get('batch') == ident
                             and r.get('sync_state') == 'fragment' and r.get('checked_at') == selected['checked_at']]
                    if len(found) != 1:
                        state = 'partial'
                        continue
                    part = json.loads(found[0]['payload'])
                    if part.get('parent') != selected['batch'] or not isinstance(part.get('items'), list):
                        state = 'partial'
                        continue
                    items.extend(part['items'])
                payload['items'] = items
            # 链接须与回执批次一起确认，而且授权范围覆盖链接所见的整张表。
            identity = payload.get('table_identity')
            allow_links = (isinstance(identity, list) and len(identity) == 2 and all(isinstance(x, str) and x for x in identity)
                           and identity == binding.get('link_source') and set(binding.get('link_shop_ids', ())) == set(shops))
            views = {k: safe_link(v) for k, v in binding.get('views', {}).items() if k in ROLES and safe_link(v)}
            return dict(source=name, state=state, batch_id=selected['batch'], checked_at=stamp(selected['checked_at']),
                        sync_state=eligible[0].get('sync_state'), payload=payload,
                        table_url=safe_link(binding.get('table_url')) if allow_links else None, views=views if allow_links else {})
        except Exception:
            return dict(source=name, state='read_failed')

    def shipping(self, shops):
        reports = [self._batch(name, shops) for name in ('shipping_orders', 'shipping_shortages')]
        if all(r['state'] == 'read_failed' for r in reports) or (
                any(r['state'] == 'read_failed' for r in reports) and not any(r.get('batch_id') for r in reports)):
            raise unavailable('本次无法读取发货监控 AI 表格')
        if all(r['state'] in ('not_bound', 'forbidden') for r in reports) and any(r['state'] == 'forbidden' for r in reports):
            raise denied()
        counts = {key: None for key in COUNTS}
        shortages = []
        batches = {r['batch_id'] for r in reports if 'batch_id' in r}
        same_batch = len(batches) == 1 and len({r.get('checked_at') for r in reports if r.get('batch_id')}) == 1
        for idx, report in enumerate(reports):
            payload = report.pop('payload', {})
            keys = COUNTS[:4] if idx == 0 else COUNTS[4:]
            raw = payload.get('counts', {})
            report['counts'] = {k: raw.get(k) if type(raw.get(k)) is int and raw[k] >= 0 else None for k in keys}
            if idx == 1:
                seen = set()
                for r in sorted(payload.get('shortage_items', []), key=lambda r: -(r.get('shortage') or 0)):
                    gid = r.get('goods_id')
                    if isinstance(gid, str) and gid.isdecimal() and gid not in seen and type(r.get('shortage')) is int and r['shortage'] > 0:
                        seen.add(gid)
                        shortages.append(dict(goods_id=gid, name=str(r.get('name') or '')[:300], shortage=r['shortage'],
                                              incoming=r['incoming'] if type(r.get('incoming')) is int else None))
                        if len(shortages) >= 5:
                            break
                report['shortage_items'] = shortages
            if same_batch:
                counts.update(report['counts'])
        outcome = ('not_bound' if all(r['state'] == 'not_bound' for r in reports) else
                   'no_report' if all(r['state'] in ('not_bound', 'no_report', 'forbidden') for r in reports) else
                   'ready' if same_batch and all(r['state'] == 'verified' for r in reports) and None not in counts.values() else 'partial')
        return dict(outcome=outcome, coverage='complete' if outcome == 'ready' else 'partial',
                    report_id=next(iter(batches)) if same_batch else None,
                    checked_at=next((r.get('checked_at') for r in reports if r.get('checked_at') is not None), None) if same_batch else None,
                    shop_ids=sorted(shops), tables=reports, counts=counts, shortage_items=shortages if same_batch else [])

    def front(self, shop, args):
        report = self._batch('front_stock', {shop})
        if report['state'] == 'read_failed':
            raise unavailable('本次无法读取前台库存 AI 表格')
        if report['state'] == 'forbidden':
            raise denied()
        payload = report.pop('payload', {})
        items = {}
        conflicts = set()
        public = ('spu', 'sku_id', 'product_name', 'config_name', 'platform_stock', 'parts_state', 'replenish_state',
                  'handling_state', 'snapshot_at', 'parts_checked_at')
        for row in payload.get('items', []):
            if row.get('shop_id') != shop or row.get('active') is not True:
                continue
            if any(args.get(k) and row.get(k) != args[k] for k in ('spu', 'sku_id')):
                continue
            key = (row.get('spu'), row.get('sku_id'))
            if not all(isinstance(x, str) and x for x in key):
                continue
            item = {k: row.get(k) for k in public}
            item['platform_stock'] = item['platform_stock'] if type(item['platform_stock']) is int and item['platform_stock'] >= 0 else None
            if key in items and items[key] != item:
                conflicts.add(key)
            items[key] = item
        for key in conflicts:
            del items[key]
        partial = report['state'] not in ('verified', 'no_report', 'not_bound') or bool(conflicts)
        return dict(shop_id=shop, snapshot_id=report.get('batch_id'), checked_at=report.get('checked_at'),
                    sync_state=report.get('sync_state', report['state']), coverage='partial' if partial else 'complete',
                    outcome='partial' if partial else 'not_bound' if report['state'] == 'not_bound' else 'ready' if items else 'no_report',
                    counts=dict(total=len(items), shown=min(len(items), 5)), items=[items[k] for k in sorted(items)[:5]],
                    table_url=report.get('table_url'), **{k: args[k] for k in ('spu', 'sku_id') if k in args})
