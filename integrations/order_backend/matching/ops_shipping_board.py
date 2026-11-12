"""按现有发货扫描扩展在办看板；只更新程序字段，保留人工状态。"""
import json
from pathlib import Path

ROLES = {
    '客服': ('web_order_numbers', 'deadline', 'urgency', 'refund', 'latest_event', 'parcels'),
    '操作': ('erp_no', 'erp_status', 'stock_status', 'incoming', 'urgency'),
    '库房打包': ('outbound_numbers', 'stock_status', 'pack_events', 'parcels'),
    '系统装机': ('outbound_numbers', 'stock_status', 'assembly_events', 'test_events'),
}
PUBLIC_FIELDS = frozenset(('key', 'shop_id', 'erp_no', 'web_order_numbers', 'outbound_numbers', 'erp_status',
                           'deadline', 'urgency', 'refund', 'stock_status', 'incoming', 'latest_event',
                           'events', 'pack_events', 'assembly_events', 'test_events', 'parcels',
                           'tracking_state', 'checked_at', 'batch_id', 'lifecycle'))


def key(row):
    return json.dumps([row['shop_id'], row['erp_no']], ensure_ascii=False, separators=(',', ':'))


def safe_parcels(route):
    """只带已核实运单和揽收证据；不透传查询请求或用户联系字段。"""
    evidence = {r['number']: r for r in route.get('evidence', [])}
    return [dict(number=n, collected=evidence.get(n, {}).get('collected') is True,
                 status=evidence.get(n, {}).get('status', '待核实')) for n in route.get('numbers', [])]


def reconcile(pending, remote, *, batch_id, checked_at, check, scoped, complete):
    """返回对稳定业务键的差量；未核实订单保留在办，已结束行保留历史。"""
    existing = {}
    for row in remote:
        ident = key(row)
        if ident in existing:
            raise ValueError('发货看板存在重复业务键')
        existing[ident] = row
    desired = {}
    for row in pending:
        ident = key(row)
        if ident in desired or row['shop_id'] not in scoped or not row['erp_no'].startswith('NO-'):
            raise ValueError('待发扫描订单身份无效或重复')
        desired[ident] = {k: row[k] for k in PUBLIC_FIELDS if k in row}
        desired[ident].update(lifecycle='in_progress', tracking_state='待核实', key=ident)
    if complete:
        for ident, row in existing.items():
            if ident not in desired and row['shop_id'] in scoped and row.get('lifecycle') != 'ended':
                desired[ident] = dict(key=ident, shop_id=row['shop_id'], erp_no=row['erp_no'], lifecycle='in_progress')
    for ident, row in desired.items():
        try:
            result = check(row['shop_id'], row['erp_no'])
            route = result.get('route') or {}
            parcels = safe_parcels(route)
            ended = result.get('business_ended') is True or (route.get('complete') is True and bool(parcels)
                                                            and all(p['collected'] for p in parcels))
            row.update({k: result[k] for k in PUBLIC_FIELDS & result.keys() if k in (
                'outbound_numbers', 'latest_event', 'events', 'pack_events', 'assembly_events', 'test_events')})
            if parcels:
                row['parcels'] = parcels
            row.update(tracking_state=str(route.get('status') or '待核实'),
                       lifecycle='ended' if ended else 'in_progress')
        except Exception:
            # 核验失败不抹掉上轮已核实工序和包裹，也不把订单标成已结束。
            row['tracking_state'] = '物流或工序待核实'
        row.update(batch_id=batch_id, checked_at=checked_at)
    return [(existing.get(ident, {}).get('record_id'), row) for ident, row in sorted(desired.items())]


def scan_for_board(job, root, now, scan, source_factory, pending_labels):
    """复用原扫描的全量分页与库存口径，同时保留正常待发订单供看板使用。"""
    from order_status_bridge import web_numbers
    from datetime import datetime
    captured, parts_by_order = [], {}

    class Source:
        def __init__(self, shop):
            self.shop = shop
            self.delegate = source_factory(shop)

        def __getattr__(self, key):
            return getattr(self.delegate, key)

        def orders(self, shop, clock):
            rows = self.delegate.orders(shop, clock)
            captured.extend((shop, row) for row in rows)
            return rows

        def detail(self, code):
            # 复用本轮已读配件清单，正常待发订单不触发额外库存查询。
            parts = self.delegate.detail(code)
            parts_by_order[(self.shop, code)] = [str(p['goodsId']) for p in parts]
            return parts

    report = scan(job, root, now=now, source_factory=Source)
    urgent = {(r['shopId'], r['code']): r for r in report['orders']}
    pending = []
    inventory = {str(r['goodsId']): r for r in report.get('stockSummary', [])}
    for shop, raw in captured:
        no = str(raw.get('c_billcode') or '').strip()
        row = urgent.get((shop, no), {})
        due = row.get('deadline') or raw.get('c_plan_delivery_date')
        if due:
            try:
                due = datetime.fromisoformat(str(due)).isoformat()
            except ValueError:
                due = None
        parts = [inventory[gid] for gid in sorted(set(parts_by_order.get((shop, no), ()))) if gid in inventory]
        risks = [r['name'] + '：' + r['status'] for r in parts if r['status'] != '本轮未见缺口']
        summary = '；'.join(risks) if risks else '本轮未见缺口' if parts else '订单配件库存待核实'
        pending.append(dict(shop_id=shop, erp_no=no, web_order_numbers=web_numbers(raw.get('c_platform_billcode')),
                            erp_status=pending_labels.get(str(raw.get('b_dispose')), '待核实'), deadline=due,
                            urgency=row.get('level', '正常') if due else '截止时间待核实',
                            refund=str(raw.get('b_refund')) != '0',
                            stock_status=row.get('stockStatus', summary),
                            incoming=[dict(goods_id=str(r['goodsId']), quantity=r.get('allIncoming'))
                                      for r in parts if r.get('allIncoming') is None or r['allIncoming'] > 0]))
    return report, pending


def publish_report(table, binding, scope, batch, checked_at, payload, *, state='verified'):
    """批次行和字段必须由部署方预先绑定；按范围＋批次回读再补齐。"""
    fields = binding['fields']
    cells = {fields[k]: v for k, v in dict(scope=scope, batch=batch, checked_at=checked_at,
             sync_state=state, payload=json.dumps(payload, ensure_ascii=False, allow_nan=False)).items()}
    found = [r for r in table.records() if r.get('cells', {}).get(fields['scope']) == scope
             and r['cells'].get(fields['batch']) == batch]
    if len(found) > 1:
        raise ValueError('发货批次存在重复')
    return table.write(found[0]['recordId'] if found else None, cells)


def sync_board(binding, pending, report, shops, table_factory, check):
    """由原发货任务调用；视图及程序字段的 ID 在试点表中核验后绑定。"""
    from collections import Counter
    tables = binding['tables']
    board = table_factory(tables['board'])
    fields = tables['board']['fields']
    if set(fields) - PUBLIC_FIELDS or not {'key', 'shop_id', 'erp_no', 'lifecycle'} <= fields.keys():
        raise ValueError('发货看板字段不符合写入范围')
    remote = []
    for raw in board.records():
        cells = raw['cells']
        row = {k: cells.get(fields[k]) for k in ('shop_id', 'erp_no', 'lifecycle')}
        if not row['shop_id'] or not row['erp_no'] or cells.get(fields['key']) != key(row):
            raise ValueError('发货看板已有行的身份无法核对')
        remote.append(dict(row, record_id=raw['recordId']))
    batch, checked = report['jobId'], report['checkedAt']
    changes = reconcile(pending, remote, batch_id=batch, checked_at=checked, check=check, scoped=shops, complete=report['complete'])
    for record_id, row in changes:
        # 只提交程序字段。人工备注、处理状态及负责人保持原值。
        cells = {fields[k]: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                 for k, v in row.items() if k in fields}
        board.write(record_id, cells)
    scopes = {tuple(sorted(shops)), *((shop,) for shop in shops)}
    for selected in sorted(scopes):
        selected_rows = [r for r in pending if r['shop_id'] in selected]
        counts = Counter(r['urgency'] for r in selected_rows)
        publish_report(table_factory(tables['orders_report']), tables['orders_report'], ','.join(selected), batch, checked,
                       dict(complete=report['complete'], table_identity=[tables['board']['baseId'], tables['board']['tableId']],
                            counts=dict(pending_scanned=len(selected_rows), overdue=counts['已超时'],
                            due_within_48h=counts['紧急'], deadline_unknown=counts['截止时间待核实'])))
    return {'status': 'verified', 'batch': batch, 'rows': len(changes),
            'enabled': True, 'complete': report['complete'], 'updated': len(changes),
            'deleted': 0, 'retained': len(changes), 'errors': [], 'url': tables['board'].get('url', '')}


def configuration(root):
    path = Path(root) / 'ops-shipping-board.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('enabled') is not True:
        return None
    tables = value['tables']
    if not {'board', 'orders_report'} <= tables.keys():
        raise ValueError('发货看板尚未核实必需表格')
    destinations = {(row['baseId'], row['tableId']) for row in tables.values()}
    if len(destinations) != len(tables) or len({row['profile'] for row in tables.values()}) != 1:
        raise ValueError('看板与批次表必须在同组织且分别绑定')
    return value



def publish_board(root, binding, pending, report, scope):
    from shipping_online import Logistics, Table, read_json
    from order_status_bridge import query_verified_orders
    from datetime import datetime
    legacy = read_json(root / 'shipping-online.json', {})
    logistics = Logistics(root, legacy) if legacy.get('enabled') and legacy.get('routeUrl') else None

    def check(shop, code):
        route = logistics.check(shop, code) if logistics else {}
        # 顺丰与工序各自取证；登记不可读时保留原工序。
        result = {'route': route}
        try:
            query = query_verified_orders('NO单号', code, [shop])
            if query['status'] == 'ok' and query['orders']:
                events = [{k: r[k] for k in ('step', 'occurred_at', 'reference_number')}
                          for r in query['orders'][0]['events']]
                def parsed(r):
                    try:
                        return datetime.fromisoformat(r['occurred_at'])
                    except (ValueError, TypeError):
                        return None
                timed = [r for r in events if parsed(r) is not None]
                result.update(events=events, latest_event=timed[-1] if timed else None,
                              outbound_numbers=list(dict.fromkeys(r['reference_number'] for r in events
                                                    if str(r['reference_number']).startswith('CK-'))),
                              pack_events=[r for r in events if r['step'] == '打包'],
                              assembly_events=[r for r in events if r['step'] == '装机'],
                              test_events=[r for r in events if r['step'] == '系统测试'])
        except Exception:
            result['route'] = dict(route, status=(str(route.get('status') or '揽收待核实') + '；工序待核实'))
        return result

    try:
        report['onlineSync'] = sync_board(binding, pending, report, scope, lambda cfg: Table(cfg, root), check)
    except Exception:
        report['onlineSync'] = dict(enabled=True, complete=False, errors=['发货看板同步待核对'])
        try:
            target = binding['tables']['orders_report']
            publish_report(Table(target, root), target, ','.join(sorted(scope)), report['jobId'],
                           report['checkedAt'], {}, state='unknown')
        except Exception:
            pass


def publish_shortages(root, binding, report, scope):
    from shipping_online import Table, read_json
    target = binding['tables'].get('shortages_report')
    if not target:
        return
    try:
        synced = report.get('shortageSync', {})
        if synced.get('complete') is not True:
            publish_report(Table(target, root), target, ','.join(sorted(scope)), report['jobId'],
                           report['checkedAt'], {}, state='unknown')
            return
        source = report['stockSummary']
        legacy = read_json(root / 'shipping-online.json', {})
        if legacy.get('profile') != target.get('profile'):
            raise ValueError('发货缺货表与批次表组织不一致')
        identity = [legacy.get('baseId'), legacy.get('shortageTable', {}).get('tableId')]
        def count(value):
            return int(value) if type(value) in (int, float) and value >= 0 and int(value) == value else None
        items = [dict(goods_id=str(r['goodsId']), name=r['name'], shortage=count(max(r.get('gap') or 0, r.get('physicalGap') or 0)),
                      incoming=count(r.get('allIncoming'))) for r in source if r['status'] == '缺货']
        payload = dict(complete=report['complete'], table_identity=identity, counts=dict(shortage_goods=len(items),
                       inventory_unknown_goods=sum(r['status'] == '库存待核实' for r in source)),
                       shortage_items=items[:20])
        publish_report(Table(target, root), target, ','.join(sorted(scope)), report['jobId'], report['checkedAt'], payload)
    except Exception:
        report['opsShortageReport'] = {'status': 'unknown'}
