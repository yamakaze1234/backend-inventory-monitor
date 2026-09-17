"""Destination-verified DingTalk delivery with a durable, conservative outbox.

No sender is created by default. Runtime must explicitly opt in with live=True;
tests inject sender(body) -> {'status': 'sent'|'failed'|'unknown'}.
"""
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta


LOCAL_TIME = timezone(timedelta(hours=8), 'Asia/Shanghai')


class DwsCommandError(Exception):
    """Retain only documented classification fields; never raw CLI diagnostics."""
    def __init__(self, error):
        self.category = error.get('category')
        self.reason = error.get('reason')
        self.retryable = error.get('retryable') is True
        delay = error.get('retry_after_seconds', 0)
        self.retry_after = float(delay) if isinstance(delay, (int, float)) and math.isfinite(delay) and delay >= 0 else 0
        super().__init__('DWS 操作未完成')


def delivery_setup(service):
    """Reuse setup/profile resolution and the existing PyInstaller resource path."""
    from monitor_setup_service import SetupService
    from monitor_portable_install import bundled_dws

    class DeliverySetup(SetupService):
        def cli(self, args, profile=None, timeout=60):
            # SetupService intentionally hides all stderr. Delivery additionally
            # needs the documented pre-write rejection category for safe retry.
            command = [self.dws_path, *args, '--format', 'json']
            if profile:
                command += ['--profile', profile]
            result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=timeout)
            if result.returncode:
                try:
                    failure = json.loads(result.stderr).get('error', {})
                except (ValueError, AttributeError):
                    failure = {}
                raise DwsCommandError(failure if isinstance(failure, dict) else {})
            try:
                data = json.loads(result.stdout)
            except ValueError:
                raise DwsCommandError({}) from None
            if not isinstance(data, dict):
                raise DwsCommandError({})
            if isinstance(data.get('error'), dict):
                raise DwsCommandError(data['error'])
            if data.get('success') is False:
                raise DwsCommandError({})
            return data

    return DeliverySetup(service.root, dws_path=bundled_dws(), portable=bool(getattr(sys, 'frozen', False)))


def update_delivery_status(service, db=None):
    def update(connection):
        counts = dict(connection.execute('SELECT status,COUNT(*) FROM events GROUP BY status'))
        service.put(connection, 'delivery_counts', counts)
        parts = []
        if counts.get('unknown'):
            parts.append(f"{counts['unknown']} 条投递结果待核实，不自动重发")
        if counts.get('partial'):
            parts.append(f"{counts['partial']} 条通知仅部分机器人发送成功，请查看通知记录")
        if counts.get('failed'):
            parts.append(f"{counts['failed']} 条通知发送失败，请查看通知记录")
        service.put(connection, 'delivery_error', '；'.join(parts))
    if db is not None:
        update(db)
    else:
        with service.db() as connection:
            update(connection)


def local_time(value):
    try:
        return datetime.fromtimestamp(float(value), LOCAL_TIME).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, OverflowError, OSError):
        return '时间未知'


def resolve_destination(service, setup=None):
    setup = setup or delivery_setup(service)
    with service.db() as db:
        previous = service.get(db, 'destination')
    profiles = setup.profiles()
    if previous and previous.get('profile'):
        candidates = [p for p in profiles if p['key'] == previous['profile']]
    else:
        candidates = [p for p in profiles if p.get('current') is True]
    if len(candidates) != 1:
        raise ValueError('库存通知账号未唯一确定，请核对钉钉当前默认账号。')
    profile = candidates[0]['key']
    result = setup.cli(['chat', '+chat-search', '--query', '示例库存通知群', '--page-all', '--page-limit', '10'], profile=profile)
    if result.get('complete') is not True or result.get('partial') or result.get('hasMore') or result.get('failures'):
        raise ValueError('示例库存通知群搜索未完整完成，库存通知保持待发送。')
    matches = {g['openConversationId']: g for g in result.get('chats', [])
               if g.get('title', g.get('name')) == '示例库存通知群' and g.get('openConversationId')}
    if len(matches) != 1:
        raise ValueError('示例库存通知群未唯一匹配，库存通知保持待发送。')
    ident = next(iter(matches))
    if previous and previous.get('group_id') and previous['group_id'] != ident:
        raise ValueError('示例库存通知群标识发生变化，库存通知暂停，请核对原接收群。')
    metadata = dict(name='示例库存通知群', group_id=ident, profile=profile, verified_at=time.time())
    with service.db() as db:
        service.put(db, 'destination', metadata)
    return metadata


def _quantity(product, key):
    return product.get(key) if product.get(key) is not None else '未知'


def _warehouse(product):
    return product.get('stock_basis') == 'warehouse_stock'


def _monitor_qty(product):
    return product.get('monitor_qty' if _warehouse(product) else 'able')


def _still_low(product, threshold):
    qty = _monitor_qty(product)
    return qty is not None and qty <= threshold


def format_events(events, service, now=None):
    now = time.time() if now is None else now
    order = {'negative_stock': 0, 'oversold': 0, 'out_of_stock': 0, 'low': 1, 'inbound': 2, 'stock_increase': 2, 'pending_inbound': 2, 'recovery': 3}
    labels = dict(negative_stock='负库存', oversold='超售', out_of_stock='库存归零预警', low='低库存', inbound='确认入库', stock_increase='库存增加', pending_inbound='待入库提醒', recovery='风险恢复')
    parts = ['# 库存提醒' if any(_warehouse(e.get('after') or {}) or (e.get('after') or {}).get('pending_source') == 'erp_total' for e in events) else '# 公司大库 · 库存提醒']
    products = {p['sku']: p for p in service.products()}
    markers = {'negative_stock': '🟥', 'oversold': '🟥', 'out_of_stock': '🟥', 'low': '🟧', 'inbound': '🟦', 'stock_increase': '🟦', 'pending_inbound': '🟪', 'recovery': '🟩'}
    last_heading = None
    def group_key(event):
        kind = event['kind']
        still_low = kind == 'recovery' and _still_low(event.get('after') or {}, event['threshold'])
        return (order.get(kind, 9), kind, still_low)
    for event in sorted(events, key=group_key):
        current, previous = event.get('after') or {}, event.get('before') or {}
        kind = event['kind']
        label = labels.get(kind, kind)
        if kind == 'recovery' and _still_low(current, event['threshold']):
            label = '负库存解除，仍低库存' if _warehouse(current) else '超售解除，仍低库存'
        marker = '🟧' if label.endswith('解除，仍低库存') else markers.get(kind, '⬜')
        goods_id = current.get('goods_id') or event.get('goods_id') or products.get(event['sku'], {}).get('goods_id')
        heading = f"## {marker} {label}"
        if heading != last_heading:
            parts.append(heading)
            last_heading = heading
        parts.append(f"**{event['name']}**")
        if kind == 'out_of_stock':
            parts.append('**分库库存已降至 0，请检查相关配置和商品链接。**' if _warehouse(current) else '**可销库存已降至 0，请检查相关配置，并下架相关商品链接。**')
        if goods_id:
            parts.append(f"商品编号：{goods_id}")
        if current.get('pending_source') == 'erp_total':
            parts += [f"**ERP 总待入：{_quantity(current, 'purchase')} 件 · 仓库未确认**",
                      f"上次总待入：{_quantity(previous, 'purchase')} 件",
                      f"采集时间：{local_time(event['observed_at'])}",
                      '此数量来自库存查询总览，不代表所选仓库待入，也不代表已入库。', '---']
            continue
        if _warehouse(current):
            parts += [f"**{current.get('warehouse_name') or event.get('warehouse') or '指定分库'} · 库存：{_quantity(current, 'monitor_qty')}　｜　预警值：{event['threshold']}**",
                      f"分库待入库：{_quantity(current, 'warehouse_purchase')}",
                      f"采集时间：{local_time(event['observed_at'])}"]
        else:
            parts += [f"**可销库存：{current.get('able', '未知')}　｜　预警值：{event['threshold']}**",
                  f"实际库存：{current.get('stock', '未知')}　·　待入库：{current.get('purchase', '未知')}",
                  f"采集时间：{local_time(event['observed_at'])}"]
        if kind == 'pending_inbound' and _warehouse(current):
            parts.append(f"**分库待入库：{_quantity(current, 'warehouse_purchase')}**（上次：{_quantity(previous, 'warehouse_purchase')}）")
        elif kind == 'pending_inbound':
            parts.append(f"**待入库：{current.get('purchase', '未知')}**（上次：{previous.get('purchase', 0)}）")
        if previous and _warehouse(current):
            parts.append(f"上次分库库存：{_quantity(previous, 'monitor_qty')}")
        elif previous:
            parts.append(f"上次可销：{previous.get('able', '未知')}；上次实际库存：{previous.get('stock', '未知')}")
        if kind == 'stock_increase':
            parts.append('库存增加不等同于确认入库；当前依据实际库存快照变化，本轮没有对应的新入库单确认。')
        inbound = current.get('inbound')
        if kind == 'inbound' and isinstance(inbound, dict):
            parts.append(f"入库单：{inbound.get('bill_code', '未知')}；类型：{inbound.get('kind', '未知')}；入库数量：{inbound.get('in_qty', '未知')}")
            parts.append(f"实际入库时间：{local_time(inbound.get('created_at'))}")
        if event.get('cause') == 'threshold_changed':
            parts.append('触发原因：预警值配置调整，非库存变化。')
        for message in service.related_messages(event['sku'], now=now):
            age = max(0, now - message['created_at'])
            age_text = f'{age / 86400:.1f} 天前' if age >= 86400 else f'{age / 3600:.1f} 小时前'
            parts += [f"经理消息：{message.get('group', '')} · {message.get('sender', '')} · {local_time(message['created_at'])}（{age_text}）",
                      '\n'.join('> ' + line for line in message['text'].splitlines()),
                      '经理消息仅作补充陈述，不构成 ERP 入库确认。']
        parts.append('---')
    return '\n\n'.join(parts)


def format_inventory_report(status, refresh_pending=False):
    """Manual detection reports share the robot presentation rules; never show SKU."""
    products = [p for p in status.get('products', []) if p.get('enabled', True)]
    order = {'negative_stock': 0, 'oversold': 0, 'low': 1, 'unknown': 2, 'normal': 3}
    labels = {'negative_stock': '🟥 负库存', 'oversold': '🟥 超售', 'low': '🟧 低库存', 'normal': '🟩 库存正常', 'unknown': '⬜ 待检查'}
    parts = ['# 库存检测' if any(_warehouse(p) for p in products) else '# 公司大库 · 库存检测', f"关注产品：{len(products)} 件", f"采集时间：{local_time(status['checked_at'])}" if status.get('checked_at') else '尚未取得库存快照']
    if refresh_pending:
        parts.append('本次刷新尚未完成，以下为最近一次成功采集结果。')
    last_heading = None
    for product in sorted(products, key=lambda p: order.get(p.get('risk'), 2)):
        heading = '## ' + labels.get(product.get('risk'), '⬜ 待检查')
        parts.append('---')
        if heading != last_heading:
            parts.append(heading)
            last_heading = heading
        parts.append('**' + product['name'] + '**')
        if product.get('goods_id'):
            parts.append(f"商品编号：{product['goods_id']}")
        qty = lambda key: product.get(key) if product.get(key) is not None else '未知'
        if _warehouse(product):
            parts += [f"**{product.get('warehouse_name') or '指定分库'} · 库存：{qty('monitor_qty')}　｜　预警值：{product['threshold']}**",
                      f"分库待入库：{qty('warehouse_purchase')}"]
        else:
            parts += [f"**可销库存：{qty('able')}　｜　预警值：{product['threshold']}**",
                  f"实际库存：{qty('stock')}　·　待入库：{qty('purchase')}"]
        if product.get('pending_source') == 'erp_total' and product.get('pending_quantity') is not None:
            parts.append(f"ERP 总待入：{product['pending_quantity']} 件 · 仓库未确认（不替代分库待入）")
    return '\n\n'.join(parts)


class DwsSender:
    def __init__(self, service, setup=None):
        self.setup = setup or delivery_setup(service)
        self.destination = resolve_destination(service, self.setup)

    def __call__(self, body):
        # The user has authorized actual inventory events to this verified group.
        try:
            result = self.setup.cli(['chat', '+send-to-group', '--group', self.destination['group_id'],
                                     '--content', body, '--yes'], profile=self.destination['profile'])
        except DwsCommandError as error:
            # Documented validation/discovery/confirmation errors occur before
            # executing the write. API/internal/transport failures do not prove
            # rejection; undocumented sendStatus values are deliberately ignored.
            definite = error.category in ('validation', 'discovery') or error.reason == 'confirmation_required'
            return dict(status='failed' if definite else 'unknown', retryable=error.retryable,
                        retry_after=error.retry_after, max_attempts=2)
        task = result.get('openTaskId')
        if task:
            try:
                result = self.setup.cli(['chat', 'message', 'query-send-status', '--open-task-id', task],
                                        profile=self.destination['profile'])
            except Exception:
                return {'status': 'unknown', 'openTaskId': task}
        # Exit code, an accepted task ID or an undocumented numeric status alone
        # cannot prove delivery. The read contract provides these IDs on success.
        success = bool(result.get('openMessageId') and result.get('openConversationId') == self.destination['group_id'])
        return dict(status='sent' if success else 'unknown', openTaskId=task,
                    openMessageId=result.get('openMessageId'))


def configure_robot(service, webhook):
    from notification_robots import store_for_service
    store = store_for_service(service)
    old = next((r for r in store.list_robots() if r['id'] == 'legacy-inventory'), None)
    ident = store.save('库存与日报机器人', webhook, ['inventory', 'daily'], robot_id=old['id'] if old else None)
    with service.db() as db:
        service.put(db, 'robot_destination', {'name': '多机器人', 'kind': 'robot', 'configured': True})
    return ident


class RobotSender:
    """Robot only. The durable router never falls back to a personal identity."""
    def __init__(self, service):
        from notification_robots import store_for_service
        self.store = store_for_service(service)

    def send(self, channel, key, body, occurred_at):
        from notification_robots import CHANNELS
        payload = {'msgtype': 'markdown', 'markdown': {'title': CHANNELS[channel], 'text': body}}
        return self.store.send(channel, key, payload, occurred_at)

    def __call__(self, body):
        import uuid
        return self.send('inventory', uuid.uuid4().hex, body, time.time())


class DeliveryWorker:
    def __init__(self, service, sender):
        self.service, self.sender = service, sender

    def run_once(self, now=None):
        now = time.time() if now is None else now
        with self.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.service.get(db, 'enabled', True):
                return 0
            rows = db.execute("SELECT id,data,attempts,created FROM events WHERE status='pending' OR (status IN ('failed','partial') AND retry_at<=?) ORDER BY created,rowid", (now,)).fetchall()
            if not rows:
                update_delivery_status(self.service, db)
                return 0
            first = json.loads(rows[0][1])
            key = lambda e: ('job', e['delivery_job']) if e.get('delivery_job') else (e.get('check_id', e['observed_at']), e.get('cause'))
            batch = [(ident, json.loads(data), attempts) for ident, data, attempts, created in rows if key(json.loads(data)) == key(first)]
            import hashlib
            job = first.get('delivery_job') or hashlib.sha256('|'.join(sorted(ident for ident, _, _ in batch)).encode()).hexdigest()
            for ident, event, _ in batch:
                event['delivery_job'] = job
                db.execute("UPDATE events SET status='sending',attempts=attempts+1,data=? WHERE id=?",
                           (json.dumps(event, ensure_ascii=False), ident))
        try:
            body = format_events([e for _, e, _ in batch], self.service, now=now)
            if isinstance(self.sender, RobotSender):
                created = min(r[3] for r in rows if any(r[0] == ident for ident, _, _ in batch))
                result = self.sender.send('inventory', job, body, created)
            else:
                result = self.sender(body)
            status = result.get('status', 'unknown') if isinstance(result, dict) else 'unknown'
            if status not in ('sent', 'failed', 'unknown', 'partial', 'skipped'):
                status = 'unknown'
        except Exception:
            status, result = 'unknown', {}
        with self.service.db() as db:
            for ident, event, attempts in batch:
                event['delivery'] = {key: result[key] for key in ('openTaskId', 'openMessageId', 'recipients') if isinstance(result, dict) and result.get(key)}
                event['delivery']['updated_at'] = now
                retry_at = 0
                if status in ('failed', 'partial'):
                    allowed = result.get('retryable', True) and attempts + 1 < result.get('max_attempts', 1000000)
                    retry_at = now + max(result.get('retry_after', 0), min(3600, 30 * 2 ** min(attempts, 7))) if allowed else float('inf')
                    event['delivery']['retryable'] = bool(allowed)
                db.execute('UPDATE events SET status=?,retry_at=?,data=? WHERE id=?',
                           (status, retry_at, json.dumps(event, ensure_ascii=False), ident))
            update_delivery_status(self.service, db)
        return len(batch)


class DeliveryHandle:
    def __init__(self, service, sender=None, live=False):
        self.service, self.sender, self.live = service, sender, live
        self.stop = threading.Event()
        self.thread = None
        self.lock = None
        if sender is None and not live:
            return
        self.lock = (service.root / 'inventory_delivery.lock').open('a+b')
        try:
            if os.fstat(self.lock.fileno()).st_size == 0:
                self.lock.write(b'0'); self.lock.flush()
            self.lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close(); self.lock = None
            return
        # Exclusive process owner only: another service instance must never
        # recover a currently running sender's claims.
        with service.db() as db:
            db.execute("UPDATE events SET status='unknown' WHERE status='sending'")
        self.thread = threading.Thread(target=self.run, daemon=True, name='inventory-delivery')
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    if self.sender is None:
                        self.sender = RobotSender(self.service)
                    from inventory_connection_alert import run_once as check_connection
                    check_connection(self.service, self.sender)
                    DeliveryWorker(self.service, self.sender).run_once()
                    from inventory_daily import run_once
                    run_once(self.service, self.sender)
                    update_delivery_status(self.service)
                except Exception as error:
                    with self.service.db() as db:
                        # Arbitrary CLI responses and exception text may contain secrets.
                        self.service.put(db, 'delivery_error', '库存通知暂不可用：' + type(error).__name__)
                self.stop.wait(10)
        finally:
            if self.lock:
                self.lock.close(); self.lock = None

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)


def start_delivery(service, *, sender=None, live=False):
    return DeliveryHandle(service, sender=sender, live=live)
