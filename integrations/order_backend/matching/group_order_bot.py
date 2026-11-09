"""钉钉订单匹配任务队列：持久保存任务状态，复用现有匹配核心执行业务。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from pathlib import Path

from patrol_service import single_instance
from patrol_worker import BatchMatchAdapter, PatrolWorker
from task_store import TaskStore
from robot_config import get_config

ROOT = Path(__file__).resolve().parent
from order_department_runtime import settings as department_settings
DEPARTMENT = department_settings()
STATE = Path(DEPARTMENT['state_root'])
DWS = DEPARTMENT['dws_path']
SHOPS = tuple(item['id'] for item in DEPARTMENT['shops'])
NAMES = {item['id']:item['name'] for item in DEPARTMENT['shops']}
INTERVAL = 7200
# 兼容旧的重匹配执行器：复用它已核对的网店单号，避免为通知再查一次 ERP。
_resolved_numbers = ContextVar('resolved_web_order_numbers', default=None)


def now_cn():
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def safe_error(error):
    # 对返回页面或群消息的异常进行脱敏，避免泄露接口地址和凭据。
    text = str(error)
    text = re.sub(r'https?://\S+', '[接口地址]', text)
    return re.sub(r'(?i)(cookie|token|secret|authorization|password)\s*[:=]\s*\S+', '[凭据已隐藏]', text)[:500]


def queue_db(root=STATE):
    # jobs 保存执行与投递状态，settings 保存服务开关、心跳及机器人设置。
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'jobs.sqlite3', timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, number TEXT NOT NULL,
        shop TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, result TEXT NOT NULL DEFAULT '',
        delivery TEXT NOT NULL DEFAULT 'pending', evidence TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return db


def enqueue(kind, number='', shop='', origin='manual', root=STATE):
    # 这里只校验参数并入队；queued 表示待处理，不能对外宣称匹配成功。
    if kind not in ('today', 'shipping', 'order', 'rematch_order', 'spu', 'rematch_spu', 'number') or shop not in ('', *SHOPS):
        raise ValueError('仅支持今天巡检、订单或SPU匹配，以及本部门已配置店铺')
    if kind in ('order', 'rematch_order'):
        number = str(number).strip().upper()
        if not re.fullmatch(r'(?:[1-9][0-9]{14,24}(?:A(?:-[0-9]+)?)?|NO-[A-Za-z0-9-]{4,70})', number, re.I):
            raise ValueError('请提供完整网店单号（保留 A 或 A-1 后缀）；也可明确使用 NO- 开头的 ERP 单号')
    elif kind in ('spu','rematch_spu','number'):
        number = str(number).strip()
        if not re.fullmatch(r'[1-9]\d{14,24}',number):raise ValueError('请提供完整SPU或号码')
    elif number:
        raise ValueError('今天巡检不接受订单号')
    with contextlib.closing(queue_db(root)) as db:
        db.execute('BEGIN IMMEDIATE')
        enabled = db.execute("SELECT value FROM settings WHERE key='serviceEnabled'").fetchone()
        if enabled and enabled[0] == '0':
            db.rollback()
            raise ValueError('后台程序已停止，请先打开 ERP 订单匹配机器人')
        # 同类在途任务及短时间内的重复请求复用原任务编号，避免重复排队执行。
        duplicate = db.execute("SELECT * FROM jobs WHERE kind=? AND number=? AND shop=? AND "
                               "(state IN ('queued','running','unknown') OR created>?) ORDER BY created DESC LIMIT 1",
                               (kind, number, shop, time.time() - (120 if kind == 'today' else 300))).fetchone()
        if duplicate:
            db.commit()
            return {'jobId': duplicate['id'], 'status': duplicate['state'], 'duplicate': True}
        job_id = uuid.uuid4().hex
        db.execute('INSERT INTO jobs(id,kind,number,shop,origin,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                   (job_id, kind, number, shop, origin, 'queued', time.time(), time.time()))
        db.commit()
        return {'jobId': job_id, 'status': 'queued', 'duplicate': False,
                'message': '已入队，结果将由配置的机器人发到目标群；入队不代表匹配完成。'}


def status(job_id='', root=STATE):
    # 心跳只说明工作进程在线；业务结果以任务回执及逐行回读状态为准。
    config = get_config(root)
    from shipping_monitor import config as shipping_config
    shipping = shipping_config(root)
    with contextlib.closing(queue_db(root)) as db:
        rows = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchall() if job_id else db.execute(
            'SELECT * FROM jobs ORDER BY created DESC LIMIT 5').fetchall()
        setting = dict(db.execute('SELECT key,value FROM settings').fetchall())
        heartbeat = float(setting.get('heartbeat', '0'))
        jobs = []
        for row in rows:
            item = {key: row[key] for key in ('id', 'kind', 'number', 'shop', 'state', 'created', 'delivery', 'evidence')}
            if row['result']:
                report = json.loads(row['result'])
                item['summary'] = summary_for(report)
                item['replyText'] = message_for(report)
            jobs.append(item)
        preferred = next((job for job in jobs if job.get('replyText') and (job_id or job['kind'] == 'today')), None)
        preferred = preferred or next((job for job in jobs if job.get('replyText') and job['kind'] != 'shipping'), None)
        if not preferred and not job_id:
            # 发货任务较多时，也保留匹配页最近一次结果，不受最近五条任务列表影响。
            latest = db.execute("SELECT result FROM jobs WHERE kind<>'shipping' AND result<>'' ORDER BY created DESC LIMIT 1").fetchone()
            if latest:
                preferred = {'replyText': message_for(json.loads(latest[0]))}
        shipping_row = db.execute("SELECT result,state,delivery FROM jobs WHERE kind='shipping' AND result<>'' ORDER BY created DESC LIMIT 1").fetchone()
        # 页面显示真实任务与投递状态，查询完成不代表群已收到提醒。
        shipping['replyText'] = ('任务：' + shipping_row['state'] + '｜通知：' + shipping_row['delivery'] + '\n\n' +
                                 message_for(json.loads(shipping_row['result']))) if shipping_row else '尚未执行发货监控'
        return {'shippingMonitor': shipping, 'workerOnline': time.time()-heartbeat < 90, 'intervalMinutes': config['intervalMinutes'],
                'robotConfig': config, 'robotSwitchPending': bool(setting.get('pendingRobotConfig')),
                'serviceEnabled': setting.get('serviceEnabled', '1') != '0',
                'schedulePaused': setting.get('schedulePaused', '0') == '1',
                'stopRequested': setting.get('stopRequested', '0') == '1',
                'workerStopped': setting.get('workerStopped', '0') == '1',
                'lastScheduled': float(setting.get('lastScheduled', '0')),
                'group': config['groupName'], 'shops': NAMES,
                'countingRule': '扫描、原已关联、成功、异常按商品明细行计数；查询订单数和待匹配SKU数单独标注，不能互相替代。',
                'replyText': preferred['replyText'] if preferred else '暂时没有已完成的巡检结果，任务正在排队或执行中。',
                'replyInstruction': '向群内回复时原样输出 replyText，不改写标题、字段名称、数量和单位。',
                'jobs': jobs}


def summary_for(report):
    # 主体按订单商品明细行计数，订单数和 SKU 数作为独立指标返回。
    if report.get('kind') == 'shipping':
        return {'complete': report.get('complete', False), '监控订单数': len(report.get('orders', []))}
    if report.get('manualItems') is not None:
        return {k:report[k] for k in ('complete','unit','manualItems')}
    if report.get('error'):
        return {'error': safe_error(report['error']), 'complete': False}
    return {'start': report['start'], 'end': report['end'], 'complete': report['complete'],
            'unit': '订单商品明细行', 'shops': [{
                'shop': NAMES[s['shopId']], 'scanComplete': s['scan']['complete'],
                '扫描商品行数': s['scan']['candidates'], '原已关联行数': s['alreadyLinked'],
                '本轮匹配并回读成功行数': s['verified'], '剩余异常行数': len(s['exceptions']),
                '非主机跳过行数': s.get('skippedNonHost', 0),
                '异常明细': s['exceptions'],
                **({'查询订单数': s['orderCount'], '待匹配SKU数': s['unmatchedSkuCount']} if 'orderCount' in s else {}),
            } for s in report['shops']]}


def web_order_numbers(value):
    """合单可能带有多个网店单号；拆开空格和逗号，保留完整后缀及原顺序。"""
    return tuple(dict.fromkeys(part.strip() for part in re.split(r'[,，]', value or '') if part.strip()))


def order_number_matches(number, row):
    number = number.upper()
    if number.startswith('NO-'):
        return number == row['billcode'].upper()
    return any(number in (part, re.sub(r'A(?:-[0-9]+)?$', '', part))
               for part in (part.upper() for part in web_order_numbers(row.get('platformNo'))))


def resolved_web_order_numbers(found):
    return list(dict.fromkeys(number for rows in found.values() for row in rows
                             for number in web_order_numbers(row.get('platformNo'))))


def find_orders(number, shops, start, end, adapter_factory=BatchMatchAdapter):
    """先查完候选店铺和全部分页，唯一定位订单后才允许进入匹配流程。"""
    import batch_match as core
    found = {}
    for shop in shops:
        adapter = adapter_factory()
        adapter.shop_id = shop
        with adapter:
            rows_seen, first_total, seen, matches = 0, None, set(), []
            for page in range(100):
                rows, total = adapter.gateway.orders(shop, start, end, number, page)
                if first_total is None:
                    first_total = int(total)
                if int(total) != first_total:
                    raise ValueError('订单查询总数变化，未执行，请稍后重查')
                for row in rows:
                    if row['shopId'] != shop or row['billcode'] in seen:
                        raise ValueError('订单查询店铺或分页不一致，未执行')
                    seen.add(row['billcode'])
                    if order_number_matches(number, row):
                        matches.append(row)
                rows_seen += len(rows)
                if rows_seen >= first_total:
                    break
                if not rows:
                    raise ValueError('订单分页不完整，未执行')
            else:
                raise ValueError('订单查询超过分页限制，未执行')
            if matches:
                found[shop] = matches
    if not found:
        raise ValueError('最近四天 ERP 中没有找到此订单，请核对订单号、店铺和同步状态')
    if len(found) != 1:
        raise ValueError('订单号在多家店铺命中，请指定店铺后重试')
    for rows in found.values():
        for row in rows:
            core.order_gate(row)
    captured = _resolved_numbers.get()
    if captured is not None:
        captured.extend(resolved_web_order_numbers(found))
    return found


def run_job(job, root=STATE, adapter_factory=BatchMatchAdapter, clock=now_cn):
    # SPU、重匹配及裸号码交给专用入口；普通订单与当天巡检复用巡检执行器。
    if job['kind'] in ('spu','rematch_spu','rematch_order','number'):
        from manual_match_jobs import run_manual
        if job['kind'] != 'rematch_order':
            return run_manual(job, root, adapter_factory, clock)
        numbers = []
        token = _resolved_numbers.set(numbers)
        try:
            report = run_manual(job, root, adapter_factory, clock)
            report['webOrderNumbers'] = numbers
            return report
        finally:
            _resolved_numbers.reset(token)
    if job['kind'] == 'shipping':
        from shipping_monitor import scan
        from shipping_online import sync
        report = scan(dict(job), root, now=clock())
        # 在线同步在物流核验后独立记录结果，库存查询部分失败仍保留紧急订单。
        report['onlineSync'] = sync(report, root)
        from shipping_shortage import sync_shortages
        report['shortageSync'] = sync_shortages(report, root)
        return report
    now = clock()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # 保留现用后端范围：巡检从昨天零点开始，指定订单查最近四个自然日。
    start = midnight - timedelta(days=1 if job['kind'] == 'today' else 3)
    shops = (job['shop'],) if job['shop'] else SHOPS
    factory = adapter_factory
    if job['kind'] == 'order':
        resolved = find_orders(job['number'], shops, start, now, adapter_factory)
        shops = tuple(resolved)

        class OrderAdapter(adapter_factory):
            unmatched_only = False
            def orders(self, segment_start, segment_end, page):
                # 只处理已经定位的订单快照，不把同店其他订单带入这次手动匹配。
                rows = resolved[self.shop_id]
                return rows[page*20:(page+1)*20], len(rows)
        factory = OrderAdapter
    path = root / 'runs' / (job['id'] + '.sqlite3')
    store = TaskStore(path)
    report = {'jobId': job['id'], 'kind': job['kind'], 'number': job['number'],
              'start': start.isoformat(), 'end': now.isoformat(), 'shops': [], 'complete': True}
    if job['kind'] == 'order':
        report['webOrderNumbers'] = resolved_web_order_numbers(resolved)
    try:
        worker = PatrolWorker(store, factory, page_limit=100, overlap_minutes=0, lease_seconds=3600)
        if job['kind'] == 'order':
            worker._segments = lambda begin, end: iter([(begin, end)])
        for shop in shops:
            scan = worker.scan(shop, start, now)
            item = {'shopId': shop, 'scan': scan, 'executions': []}
            if scan['complete']:
                item['classifications'] = worker.classify(scan['scanId'])
                item['executions'] = worker.execute_ready(shop, limit=1000)
            else:
                report['complete'] = False
            rows = store.db.execute('''
                SELECT c.billcode,c.platform_no,c.sku,c.spu,c.goods_id,c.classification,
                  CASE WHEN t.state IN ('needs_review','paused') THEN
                    COALESCE(NULLIF(t.reason,''),'执行未确认完成，未返回具体失败原因')
                  WHEN c.classification='ready' THEN
                    COALESCE(NULLIF(t.reason,''),'匹配尚未完成')
                  ELSE c.reason END reason,
                  t.state task_state,tc.readback_state,tc.readback_goods_id
                FROM candidates c LEFT JOIN task_candidates tc ON tc.candidate_id=c.id
                LEFT JOIN tasks t ON t.id=tc.task_id WHERE c.scan_id=?
                ORDER BY c.billcode,c.sku''', (scan['scanId'],)).fetchall()
            # 只有逐行 readback_state=verified 才算本轮成功，异常和原已一致单独统计。
            item['verified'] = sum(row['readback_state'] == 'verified' for row in rows)
            item['orderCount'] = len({row['billcode'] for row in rows})
            item['unmatchedSkuCount'] = len({row['sku'] for row in rows if row['goods_id'] in ('', '0') and row['sku']})
            item['alreadyLinked'] = sum(row['classification'] == 'ignored' for row in rows)
            item['skippedNonHost'] = sum(row['classification'] == 'non_host' for row in rows)
            item['exceptions'] = [dict(row) for row in rows if row['classification'] not in ('ignored', 'non_host')
                                  and row['readback_state'] != 'verified']
            if item['exceptions']:
                report['complete'] = False
            report['shops'].append(item)
        return report
    finally:
        store.close()


def short_match_reason(reason):
    # 群消息只简化已知原因，原始原因仍保留在任务记录。
    return safe_error({
        '订单商品行标识重复，禁止按序号自动定位': '商品行重复，需人工核对',
        '已关联套餐的配件或数量不同，请人工核验后发送重新匹配命令': '套餐不一致，核对后重新匹配',
        '订单已不存在或店铺已变化，请重新搜索': '订单不存在或店铺有变，请重查',
    }.get(reason, reason))


def match_exception_lines(rows, limit=12, compact=False):
    # ERP 单据仍用于内部归组；群里显示网店单号，合单不丢其中任一个。
    groups = {}
    for row in rows:
        reason = short_match_reason(row['reason'] or row['task_state'] or row['classification'])
        key = (row.get('billcode'), web_order_numbers(row.get('platform_no')), None if compact else row.get('spu'), reason)
        groups[key] = groups.get(key, 0) + 1
    details = []
    hidden = 0
    for index, ((_, platform_numbers, spu, reason), count) in enumerate(groups.items()):
        if index >= limit:
            hidden += count
            continue
        order = '、'.join(platform_numbers) if platform_numbers else '网店单号未知'
        if compact:
            reason = {
                '商品行重复，需人工核对': '商品行重复，需核对',
                '套餐不一致，核对后重新匹配': '套餐不一致，核对后重匹配',
            }.get(reason, reason)
        elif platform_numbers:
            order = f'网店单号 {order}'
        label = f'{order}｜SPU {spu}' if spu else order
        suffix = f'（{count} 行）' if count > 1 else ''
        details.append(f'{label}｜{reason}{suffix}')
    if hidden:
        details.append(f'另有 {hidden} 行，见本机任务记录')
    return details


def patrol_message(report):
    # 群里只展示本轮回读成功和异常行数；未扫完的店单独说明。
    end = datetime.fromisoformat(report['end'])
    lines = [f'【订单匹配巡检｜{end.month}月{end.day}日 {end:%H:%M}】', '']
    details = []
    for shop in report['shops']:
        name = NAMES[shop['shopId']]
        rows = shop['exceptions']
        lines.append(f"{name}：成功 {shop['verified']} 行｜异常 {len(rows)} 行")
        if rows or not shop['scan']['complete']:
            details.extend(['', f'{name}异常'])
            if not shop['scan']['complete']:
                details.append('- 扫描不完整，未执行匹配')
            details.extend(f'- {detail}' for detail in match_exception_lines(rows, compact=True))
    return '\n'.join(lines + details)


def order_message_identifier(report):
    """通知优先用已核对的网店单号；显式 ERP 输入只在网店单号未确认时作备用标识。"""
    if report.get('webOrderNumbers'):
        return '网店单号：' + '、'.join(report['webOrderNumbers'])
    number = report.get('number', '')
    if number.upper().startswith('NO-'):
        return '网店单号：未确认｜ERP 单号（备用）：' + number
    return '网店单号：' + (number or '未知')


def message_for(report):
    # 固定结果消息的字段与单位，状态查询和群通知共用同一份文本。
    if report.get('manualItems') is not None:
        from manual_match_jobs import message
        rendered = message(report)
        if report.get('kind') == 'rematch_order':
            # 保留原重匹配结果格式，只替换订单标识行；SPU 任务不受影响。
            lines = rendered.splitlines()
            if len(lines) > 1 and lines[1].startswith('号码：'):
                lines[1] = order_message_identifier(report)
                return '\n'.join(lines)
        return rendered
    if report.get('kind') == 'shipping':
        from shipping_monitor import message
        return message(report)
    manual = report.get('kind') in ('order','rematch_order')
    lines = ['【ERP 订单匹配结果】' if manual else '【ERP 订单匹配巡检】']
    if report.get('error'):
        if manual:
            lines.append(order_message_identifier(report))
        return '\n'.join(lines + ['执行状态：未确认完成', '异常原因：' + safe_error(report['error']),
                                  '任务编号：' + report['jobId'][:8]])
    if not manual:
        return patrol_message(report)
    start = report['start'].replace('T', ' ')[:16]
    end = report['end'].replace('T', ' ')[:16]
    range_end = end[11:] if start[:10] == end[:10] else end
    lines += ['', '巡检时间：' + end + '（北京时间） 扫描范围：' + start + ' 至 ' + range_end]
    lines.append(order_message_identifier(report))
    lines += ['', '店铺结果（单位：商品明细行）', '']
    scanned = linked = verified = exceptions = skipped = 0
    details = []
    for shop in report['shops']:
        scanned += shop['scan']['candidates']
        linked += shop['alreadyLinked']
        verified += shop['verified']
        exceptions += len(shop['exceptions'])
        skipped += shop.get('skippedNonHost', 0)
        lines.append(f"* {NAMES[shop['shopId']]}：扫描{shop['scan']['candidates']}｜原已关联{shop['alreadyLinked']}｜本轮匹配成功{shop['verified']}｜剩余异常{len(shop['exceptions'])}")
        if shop.get('skippedNonHost'):
            lines.append(f"  已核验非主机跳过：{shop['skippedNonHost']} 行")
        if 'orderCount' in shop:
            lines.append(f"  查询订单数：{shop['orderCount']} 单｜待匹配SKU数：{shop['unmatchedSkuCount']} 个")
        if not shop['scan']['complete'] or shop['exceptions']:
            details += ['', '* ' + NAMES[shop['shopId']], '']
        if not shop['scan']['complete']:
            details.append('扫描不完整，本店未执行；' + safe_error(shop['scan'].get('error') or '分页未完整返回'))
        for detail in match_exception_lines(shop['exceptions']):
            details.extend([detail, ''])
    lines += ['', f'合计：扫描{scanned}｜原已关联{linked}｜本轮匹配成功{verified}｜剩余异常{exceptions}', '', '异常明细：']
    if skipped:
        lines.insert(-2, f'已核验非主机跳过：{skipped} 行（不计入主机匹配成功或异常）')
    lines += details if details else ['无']
    return '\n'.join(lines)


def deliver(job_id, report, root=STATE, runner=subprocess.run):
    if report.get('kind') == 'shipping':
        from shipping_monitor import deliver as deliver_shipping
        return deliver_shipping(job_id, report, root, runner)
    message = message_for(report)
    with contextlib.closing(queue_db(root)) as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['delivery'] != 'pending':
            return
        # 定时巡检无新增成功且异常未变化时保持静默；手动任务仍返回结果。
        signature = hashlib.sha256(json.dumps({
            'error': report.get('error'), 'shops': [{
                'shopId': s['shopId'], 'verified': s['verified'], 'exceptions': s['exceptions'],
                'scanComplete': s['scan']['complete'], 'scanError': s['scan'].get('error')
            } for s in report.get('shops', [])]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = db.execute("SELECT value FROM settings WHERE key='lastNotification'").fetchone()
        verified = sum(s['verified'] for s in report.get('shops', []))
        actionable = report.get('error') or any(s['exceptions'] or not s['scan']['complete'] for s in report.get('shops', []))
        if job['origin'] == 'schedule' and not verified and (not actionable or (previous and previous[0] == signature)):
            db.execute("UPDATE jobs SET delivery='suppressed' WHERE id=?", (job_id,))
            return
        # 发送前将投递状态落盘为 unknown；发送超时不自动重发，避免重复通知。
        if db.execute("UPDATE jobs SET delivery='unknown' WHERE id=? AND delivery='pending'", (job_id,)).rowcount != 1:
            return
        try:
            config = get_config(root)
            response = runner([DWS, 'chat', '+messages-send', '--as', 'bot', '--robot-code', config['robotCode'],
                               '--groups', config['groupId'], '--text', message, '--profile', config['profile'],
                               '--format', 'json', '--yes'], capture_output=True, text=True, encoding='utf-8', timeout=45,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            value = json.loads(response.stdout)
            # 即使只发一个群，也按多目标投递回执中的成功数和失败数确认结果。
            if value.get('succeededCount') == 1 and value.get('failedCount') == 0:
                db.execute("UPDATE jobs SET delivery='sent',evidence=? WHERE id=?", (json.dumps(value, ensure_ascii=False), job_id))
                db.execute("INSERT OR REPLACE INTO settings VALUES('lastNotification',?)", (signature,))
            else:
                db.execute('UPDATE jobs SET evidence=? WHERE id=?', ('投递未确认，需核对钉钉回执', job_id))
        except Exception as error:
            db.execute('UPDATE jobs SET evidence=? WHERE id=?', (safe_error(error), job_id))


def service(root=STATE, once=False):
    # 单实例锁保证仅一个调度进程领取任务；每个业务任务在独立子进程中运行。
    with single_instance(root / 'worker.lock') as acquired:
        if not acquired:
            return
        with contextlib.closing(queue_db(root)) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('workerPid',?)", (str(os.getpid()),))
            db.execute("INSERT OR REPLACE INTO settings VALUES('workerStopped','0')")
            # 上次进程中断的 running 任务转为 unknown，不自动重新执行其 ERP 写入。
            db.execute("UPDATE jobs SET state='unknown',updated=? WHERE state='running'", (time.time(),))
            for interrupted in db.execute("SELECT id,kind,number FROM jobs WHERE state='unknown' AND delivery='pending'").fetchall():
                report = {'jobId': interrupted['id'], 'kind': interrupted['kind'], 'number': interrupted['number'],
                          'error': '后台上次中断，匹配结果未核实；本任务不会自动重跑'}
                db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(report, ensure_ascii=False), interrupted['id']))
                deliver(interrupted['id'], report, root)
            for finished in db.execute("SELECT id,result FROM jobs WHERE state IN ('completed','needs_review') AND delivery='pending'").fetchall():
                deliver(finished['id'], json.loads(finished['result']), root)
            while True:
                settings = dict(db.execute('SELECT key,value FROM settings').fetchall())
                if settings.get('stopRequested') == '1' or settings.get('serviceEnabled') == '0':
                    db.execute("INSERT OR REPLACE INTO settings VALUES('heartbeat','0')")
                    db.execute("INSERT OR REPLACE INTO settings VALUES('workerStopped','1')")
                    return
                # 暂停只限制定时巡检；用户明确提交的手动匹配仍可领取。
                paused = settings.get('schedulePaused') == '1'
                db.execute("INSERT OR REPLACE INTO settings VALUES('heartbeat',?)", (str(time.time()),))
                last = db.execute("SELECT value FROM settings WHERE key='lastScheduled'").fetchone()
                if not once and not paused and (not last or time.time()-float(last[0]) >= get_config(root)['intervalMinutes']*60):
                    enqueue('today', origin='schedule', root=root)
                    db.execute("INSERT OR REPLACE INTO settings VALUES('lastScheduled',?)", (str(time.time()),))
                # 发货扫描使用独立开关，不因匹配巡检暂停而停止监控。
                from shipping_monitor import config as shipping_config
                shipping = shipping_config(root)
                if not once and shipping['enabled'] and time.time()-float(settings.get('lastShippingScheduled', '0')) >= shipping['target']['intervalMinutes']*60:
                    enqueue('shipping', origin='schedule', root=root)
                    db.execute("INSERT OR REPLACE INTO settings VALUES('lastShippingScheduled',?)", (str(time.time()),))
                job = db.execute("SELECT * FROM jobs WHERE state='queued'" +
                                 (" AND (origin<>'schedule' OR kind='shipping')" if paused else '') +
                                 " ORDER BY CASE origin WHEN 'manual' THEN 0 ELSE 1 END,created LIMIT 1").fetchone()
                if job:
                    db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), job['id']))
                    # 父进程负责心跳和状态汇总，子进程负责匹配并原子写入结果文件。
                    output = root / (job['id'] + '.result.json')
                    log = root / (job['id'] + '.log')
                    with log.open('w', encoding='utf-8') as stream:
                        child = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                                                  'execute', '--job', job['id']], stdout=stream, stderr=stream,
                                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        deadline = time.monotonic() + 3600
                        while child.poll() is None and time.monotonic() < deadline:
                            db.execute("INSERT OR REPLACE INTO settings VALUES('heartbeat',?)", (str(time.time()),))
                            time.sleep(2)
                        if child.poll() is None:
                            child.terminate()
                            child.wait(timeout=15)
                    if output.exists():
                        report = json.loads(output.read_text(encoding='utf-8'))
                        state = 'completed' if report.get('complete') else 'needs_review'
                    else:
                        state = 'unknown'
                        report = {'jobId': job['id'], 'kind': job['kind'], 'number': job['number'],
                                  'error': '执行中断或超时；结果未核实，不自动重试本任务'}
                    db.execute('UPDATE jobs SET state=?,result=?,updated=? WHERE id=?',
                               (state, json.dumps(report, ensure_ascii=False), time.time(), job['id']))
                    deliver(job['id'], report, root)
                if once:
                    return
                time.sleep(2)


def main():
    # submit/status 是控制入口，serve/once 负责调度，execute 只执行已领取的任务。
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['submit', 'status', 'serve', 'once', 'execute'])
    parser.add_argument('--kind', choices=['today', 'shipping', 'order', 'rematch_order', 'spu', 'rematch_spu', 'number'], default='today')
    parser.add_argument('--number', default='')
    parser.add_argument('--shop', default='')
    parser.add_argument('--job', default='')
    args = parser.parse_args()
    if args.command == 'submit':
        result = enqueue(args.kind, args.number, args.shop)
    elif args.command == 'status':
        result = status(args.job)
    elif args.command in ('serve', 'once'):
        service(once=args.command == 'once')
        return
    else:
        with contextlib.closing(queue_db()) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND state='running'", (args.job,)).fetchone()
        if not job:
            raise ValueError('任务不存在或尚未由工作进程领取')
        try:
            result = run_job(job)
        except Exception as error:
            result = {'jobId': args.job, 'kind': job['kind'], 'number': job['number'],
                      'complete': False, 'error': safe_error(error)}
        target = STATE / (args.job + '.result.json')
        pending = target.with_suffix('.pending')
        pending.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        pending.replace(target)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': safe_error(error)}, ensure_ascii=False))
        raise SystemExit(1)
