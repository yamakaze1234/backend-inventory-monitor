"""One robot notification per ERP outage, persisted across application restarts."""
import time
import uuid


def run_once(service, sender, now=None):
    now = time.time() if now is None else now
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not service.get(db, 'enabled', True):
            return False
        heartbeat = service.get(db, 'heartbeat', 0)
        error = bool(service.get(db, 'connection_error', ''))
        state = service.get(db, 'erp_connection_alert', {})
        healthy = bool(heartbeat) and 0 <= now - heartbeat < 70 and not error
        if healthy:
            # A healthy observation rearms the next outage; no recovery push.
            if not state.get('armed') or state.get('waiting_since'):
                service.put(db, 'erp_connection_alert', dict(armed=True))
            return False
        # No previous connection is a first-run setup state, not an outage.
        if not state.get('armed', bool(heartbeat)):
            return False
        if not state.get('waiting_since'):
            state = dict(armed=True, waiting_since=now)
            service.put(db, 'erp_connection_alert', state)
        recovery = service.get(db, 'erp_recovery', {})
        failed = recovery.get('state') in ('captcha', 'exhausted') and recovery.get('at', 0) >= state['waiting_since']
        if not failed and now - state['waiting_since'] < 300:
            return False
        job = 'erp-disconnected:' + uuid.uuid4().hex
        checked = service.get(db, 'checked_at', 0)
        # Claim before any network I/O. An interrupted/unknown delivery is never
        # replayed just because another loop or application instance runs.
        state = dict(armed=False, job=job, detected_at=now, status='unknown')
        service.put(db, 'erp_connection_alert', state)
    from inventory_delivery import local_time
    reason = '自动重登需要人工验证或已达到重试上限。' if failed else '已等待自动恢复 5 分钟，ERP 连接仍未恢复（浏览器关闭时无法执行重试）。'
    body = '\n\n'.join([
        '# 库存监控 · ERP 连接断开', reason,
        '发现时间：' + local_time(now),
        '最近成功采集：' + (local_time(checked) if checked else '尚未完成'),
        '库存数据可能已过期。请检查网络、ERP 登录和“分库库存 → 公司大库”页面，并保持浏览器及库存程序运行。',
        '本次断连仅提醒一次；恢复连接后再次断开才重新提醒。',
    ])
    try:
        if hasattr(sender, 'send'):
            result = sender.send('inventory', job, body, now)
        else:
            result = sender(body)
        status = result.get('status', 'unknown') if isinstance(result, dict) else 'unknown'
        if status not in ('sent', 'failed', 'unknown', 'partial', 'skipped'):
            status = 'unknown'
    except Exception:
        status = 'unknown'
    with service.db() as db:
        current = service.get(db, 'erp_connection_alert', {})
        if current.get('job') == job:
            current.update(status=status, updated_at=now)
            service.put(db, 'erp_connection_alert', current)
    return True
