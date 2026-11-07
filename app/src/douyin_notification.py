"""按当前补库存清单投递；历史预览不入队，逐机器人持久化去重。"""
import json
import time
import uuid

from douyin_restock import brief

CHANNEL = 'douyin_inventory'


def audience(robots):
    """仅读取公开接收配置和启用时间，不读取机器人凭据。"""
    with robots.db() as db:
        rows = db.execute('SELECT id,channels,since FROM robots WHERE enabled=1 AND deleted=0').fetchall()
    return [(ident, json.loads(since)[CHANNEL]) for ident, scopes, since in rows
            if CHANNEL in json.loads(scopes)]


def deliver_current(store, robots, ident, *, online_ready=True):
    """仅在成功采集或配件核对后调用；相同清单重启不重发，未知结果不重试。"""
    results = []
    for robot, epoch in audience(robots):
        key = 'douyin_delivery:' + ident + ':' + robot
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            config = db.execute('SELECT * FROM stores WHERE id=?', (ident,)).fetchone()
            row = db.execute('SELECT data FROM restock_previews WHERE store=?', (ident,)).fetchone()
            if not config or config['deleted'] or not config['confirmed'] or not config['enabled'] or not row:
                continue
            preview = json.loads(row[0])
            payload = brief(preview, config['online_url'] if online_ready else '')
            if payload and not online_ready:
                payload['markdown']['text'] = payload['markdown']['text'].replace(
                    '在线明细暂未配置', '在线明细尚未同步，请在库存监控程序中查看本次补库存清单。')
            saved = db.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
            old = json.loads(saved[0]) if saved else {}
            same = old.get('signature') == preview['signature'] and old.get('epoch') == epoch
            # 空清单也保存，后续再次缺货才能形成新的提醒周期。
            if same and not old.get('retryable'):
                results.append(dict(robot=robot, status=old['status']))
                continue
            state = (old if same else dict(job=uuid.uuid4().hex, signature=preview['signature'],
                                          epoch=epoch, occurred=time.time(), payload=payload))
            # 明确失败后的重试沿用任务号，但使用本轮时间和在线同步状态生成的文案。
            state.update(status='sending' if payload else 'empty', retryable=False, payload=payload)
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', (key, json.dumps(state, ensure_ascii=False)))
        if not payload:
            continue
        # 先持久化发送占位；进程中断时保留未知状态，不能盲目补发。
        try:
            result = robots.send(CHANNEL, state['job'], state['payload'], state['occurred'], recipient_ids=[robot])
        except Exception:
            result = dict(status='unknown', retryable=False)
        state.update(status=result.get('status', 'unknown'), retryable=result.get('retryable') is True)
        with store.db() as db:
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', (key, json.dumps(state, ensure_ascii=False)))
        results.append(dict(robot=robot, status=state['status']))
    return results


def delivery_label(store, ident, robots=None):
    """展示真实投递状态，不能把开启接收开关等同于已发送。"""
    with store.db() as db:
        rows = db.execute('SELECT key,value FROM preferences WHERE key LIKE ?', ('douyin_delivery:' + ident + ':%',)).fetchall()
        preview = db.execute('SELECT data FROM restock_previews WHERE store=?', (ident,)).fetchone()
    signature = json.loads(preview[0])['signature'] if preview else None
    targets = dict(audience(robots)) if robots is not None else None
    states = [json.loads(value) for key, value in rows
              if json.loads(value).get('signature') == signature and
              (targets is None or targets.get(key.rsplit(':', 1)[1]) == json.loads(value).get('epoch'))]
    labels = {'sent': '已发送', 'unknown': '结果未知，不自动重发', 'sending': '发送中或结果待核对',
              'failed': '发送失败，请查看机器人通知记录', 'partial': '部分发送成功',
              'empty': '当前无补库存提醒', 'skipped': '已跳过'}
    return '；'.join(dict.fromkeys(labels.get(s['status'], '待发送') for s in states)) or '等待下一次成功检查'
