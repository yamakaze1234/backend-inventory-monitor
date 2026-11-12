"""只读发货监控：按承诺截止时间提醒，保留库存未知与负责人歧义。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from robot_config import get_config, validate

PENDING = {'0': '待客审', '6': '待打印', '9': '已打印'}
CN = timezone(timedelta(hours=8))


def config(root):
    # 独立提醒路由可覆盖匹配机器人；未设置时动态沿用当前目标。
    from group_order_bot import queue_db
    with contextlib.closing(queue_db(root)) as db:
        values = dict(db.execute('SELECT key,value FROM settings'))
    target = json.loads(values['shippingTarget']) if values.get('shippingTarget') else get_config(root)
    # 独立周期覆盖提醒路由默认值，不改变路由身份。
    target = dict(target, intervalMinutes=int(values.get('shippingIntervalMinutes') or target['intervalMinutes']))
    return {'enabled': values.get('shippingEnabled', '0') == '1',
            'target': validate(target), 'inherit': not bool(values.get('shippingTarget'))}


def identifier(value):
    return '' if value is None else str(value).strip().removesuffix('.0')


def date(value):
    # ERP 无时区日期均按北京时间解释，禁止用主机时区改变 48 小时边界。
    parsed = datetime.fromisoformat(str(value).strip())
    return parsed.replace(tzinfo=CN) if parsed.tzinfo is None else parsed.astimezone(CN)


def owner(row):
    business = str(row.get('c_handing_emp_org_name') or '').strip()
    remarks = '\n'.join(str(row.get(k) or '') for k in ('c_saller_remark', 'c_saller_memo_late'))
    # 只识别明确负责人标签或 ERP 留言签名，不把任意中文词猜成人名。
    names = re.findall(r'(?:业务员|负责人|跟单|销售)\s*[:：]\s*([\u4e00-\u9fffA-Za-z·]{2,12})', remarks)
    names += re.findall(r'【([^【】\s]{2,12})\s+\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}】', remarks)
    names = sorted(set(names))
    generic = not business or any(x in business for x in ('公用', '公共', '系统', '默认'))
    candidates = sorted(set(names + ([] if generic else [business])))
    # 签名作者只是候选；没有明确业务员时保留“待确认”，避免错派给留言编辑人。
    confirmed = not generic and all(name == business for name in names)
    return {'name': business if confirmed else '待确认' + ('（' + ' / '.join(candidates) + '）' if candidates else ''),
            'business': business, 'remarkNames': names, 'confirmed': confirmed}


def number(value):
    if value is None or value == '' or isinstance(value, bool):
        raise ValueError('库存或需求数量缺失')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('库存或需求数量无效')
    return result


class Source:
    """复用已登录的只读 ERP 传输和 SDK 字段映射，不调用任何匹配接口。"""
    def __init__(self, shop):
        from batch_match import connect
        self.client = connect(shop)

    def close(self):
        self.client.close()

    def orders(self, shop, now):
        from tiny_tools.erp import Api_网店审单
        rows, seen = [], set()
        # 发货监控覆盖历史未完成订单，不继承匹配巡检的当天支付时间限制。
        for state in PENDING:
            api = Api_网店审单(datetime(2000, 1, 1), now.replace(tzinfo=None),
                             shopId=[shop], b_dispose=0, search_b_dispose=int(state))
            for row in self.client._paged(api.base_url, api.params()):
                code = identifier(row.get('c_billcode'))
                if not code or identifier(row.get('shop_id')) != shop:
                    raise ValueError('订单编号或店铺范围无法核验')
                if identifier(row.get('b_dispose')) != state or code in seen:
                    raise ValueError('订单状态变化或重复，请重查')
                seen.add(code)
                if 'b_cancel' not in row or 'b_refund' not in row:
                    raise ValueError('订单取消或退款状态缺失')
                if identifier(row['b_cancel']) == '0':
                    rows.append(row)
        return rows

    def detail(self, code):
        raw = self.client.order_detail(code)
        heads = raw.get('data_order')
        if not isinstance(heads, list) or len(heads) != 1 or identifier(heads[0].get('c_billcode')) != code:
            raise ValueError('详情订单身份不一致')
        if identifier(heads[0].get('b_dispose')) not in PENDING:
            raise ValueError('订单状态已变化，下轮重新确认')
        rows = raw.get('data_order_deliver_goods')
        if not isinstance(rows, list) or not rows:
            raise ValueError('未生成发货商品，库存待核实')
        result = []
        for row in rows:
            gid = identifier(row.get('goods_id'))
            if gid in ('', '0'):
                gid = identifier(row.get('product_goods_id'))
            qty = number(row.get('n'))
            if gid in ('', '0') or qty <= 0:
                raise ValueError('发货配件编号或数量无效')
            result.append({'goodsId': gid, 'code': str(row.get('c_product_goods_sku') or row.get('c_goods_sku') or ''),
                           'name': str(row.get('c_product_goods_name') or row.get('c_goods_sku') or gid), 'quantity': qty})
        return result

    def stocks(self):
        from tiny_tools.erp import Api_分库库存
        api = Api_分库库存(b_stock=0, depotIds=[2])
        result = {}
        for row in self.client._paged(api.base_url, api.params(), max_rows=50000):
            if row.get('c_c_name') != '公司大库':
                raise ValueError('返回的库存不是公司大库')
            gid = identifier(row.get('goods_id'))
            if not gid or gid in result:
                raise ValueError('库存商品编号为空或重复')
            result[gid] = {'stock': row.get('n_stock'), 'available': row.get('n_stock_able'),
                           'incoming': row.get('n_stock_purchase'), 'allIncoming': row.get('n_purchase'),
                           'stockCode': str(row.get('b_c_sku') or '')}
        return result


def scan(job, root, source_factory=Source, now=None):
    from group_order_bot import SHOPS, NAMES, safe_error
    now = date(now or datetime.now(CN))
    report = {'kind': 'shipping', 'jobId': job['id'], 'checkedAt': now.isoformat(),
              'complete': True, 'orders': [], 'errors': [], 'scanned': 0,
              'stockBasis': '公司大库现货对比本次三店待发需求；可销、待入另列，不重复扣减已占用订单'}
    demand = defaultdict(float)
    # 缺货汇总包含所有待发订单，不受48小时提醒窗口影响。
    stock_orders = []
    stocks = None
    for shop in ((job['shop'],) if job.get('shop') else SHOPS):
        source = None
        try:
            source = source_factory(shop)
            rows = source.orders(shop, now)
            report['scanned'] += len(rows)
            if stocks is None:
                try:
                    stocks = source.stocks()
                except Exception as error:
                    report['errors'].append('公司大库：' + safe_error(error))
            # 单个订单详情失败不拖垮整轮；需求覆盖不完整时禁止给出“全部配齐”的结论。
            def fetch(row):
                try:
                    return source.detail(identifier(row['c_billcode'])), ''
                except Exception as error:
                    return [], safe_error(error)
            with ThreadPoolExecutor(max_workers=5) as pool:
                details = list(pool.map(fetch, rows))
            for row, (parts, error) in zip(rows, details):
                stock_orders.append({'shopId':shop, 'shop':NAMES[shop], 'code':identifier(row['c_billcode']),
                                     'state':identifier(row['b_dispose']), 'parts':parts})
                for part in parts:
                    demand[part['goodsId']] += part['quantity']
                code = identifier(row['c_billcode'])
                if error:
                    report['errors'].append(NAMES[shop] + ' ' + code + '：' + error)
                raw_due = row.get('c_plan_delivery_date')
                try:
                    due = date(raw_due)
                    hours = (due - now).total_seconds() / 3600
                    level = '已超时' if hours < 0 else '紧急' if hours <= 48 else '正常'
                except (ValueError, TypeError):
                    due, hours, level = None, None, '截止时间待核实'
                if level == '正常':
                    continue
                report['orders'].append({'shopId': shop, 'shop': NAMES[shop], 'code': code,
                    'platformNo': identifier(row.get('c_platform_billcode')), 'status': PENDING[identifier(row['b_dispose'])],
                    'deadline': due.isoformat() if due else '', 'hours': round(hours, 2) if hours is not None else None,
                    'level': level, 'owner': owner(row), 'parts': parts, 'detailError': error,
                    'refund': identifier(row.get('b_refund')) != '0'})
        except Exception as error:
            report['errors'].append(NAMES[shop] + '：' + safe_error(error))
        finally:
            if source:
                source.close()
    report['complete'] = not report['errors']
    from shipping_shortage import summarize
    report['stockSummary'] = summarize(stock_orders, stocks or {}, report['complete'])
    for order in report['orders']:
        risks = []
        own = defaultdict(float)
        for part in order['parts']:
            own[part['goodsId']] += part['quantity']
        for part in order['parts']:
            stock = (stocks or {}).get(part['goodsId'], {})
            part.update(stock)
            part['totalDemand'] = demand[part['goodsId']]
            try:
                physical, available = number(stock.get('stock')), number(stock.get('available'))
                if physical < own[part['goodsId']]:
                    state = '现货不足'
                elif physical < part['totalDemand']:
                    state = '多单争用，待分配'
                elif available < 0:
                    state = '可销为负，待核实占用'
                else:
                    state = '本轮未见缺口' if report['complete'] else '需求覆盖不完整，待核实'
            except (TypeError, ValueError):
                state = '库存待核实'
            part['assessment'] = state
            if state not in ('本轮未见缺口', '需求覆盖不完整，待核实'):
                risks.append(part['name'] + '：' + state)
        order['stockStatus'] = '；'.join(risks) or ('库存待核实：' + order['detailError'] if order['detailError'] else
            '本轮未见缺口' if report['complete'] else '已查配件未见缺口；部分订单需求待核实')
    report['orders'].sort(key=lambda x: (x['deadline'] or '9999', x['shopId'], x['code']))
    return report


def order_text(order, compact=False):
    who = order['owner']
    lines = [f"{order['level']}｜{order['shop']}｜{order['platformNo'] or order['code']}",
             f"ERP：{order['code']}｜{order['status']}｜截止：{order['deadline'][:16].replace('T', ' ') or '待核实'}",
             f"负责人：{who['name']}｜业务员：{who['business'] or '空'}｜备注候选：{' / '.join(who['remarkNames']) or '无'}",
             '库存：' + order['stockStatus']]
    if order.get('refund'):
        lines.append('存在退款标记，需先核实履约状态')
    for part in order['parts']:
        if compact and part['assessment'] in ('本轮未见缺口', '需求覆盖不完整，待核实'):
            continue
        lines.append(f"  {part['name']}：本单{part['quantity']:g} / 三店待发{part['totalDemand']:g} / 现货{part.get('stock', '未知')} / 可销{part.get('available', '未知')} / 待入{part.get('incoming', '未知')}；{part['assessment']}")
    return '\n'.join(lines)


def notification_summary(report):
    """群里只展示汇总，订单及配件明细保留在在线表和工作台。"""
    orders=report.get('orders',[])
    lines=['# 发货监控提醒','**待发扫描 %s 单 · 超时 %s 单 · 紧急 %s 单**' % (
        report.get('scanned',0),sum(x.get('level')=='已超时' for x in orders),sum(x.get('level')=='紧急' for x in orders))]
    for shop in sorted({x.get('shop','未知店铺') for x in orders}):
        rows=[x for x in orders if x.get('shop','未知店铺')==shop]
        lines.append('• %s：超时 %s 单，紧急 %s 单，截止待核实 %s 单。' % (shop,
            sum(x.get('level')=='已超时' for x in rows),sum(x.get('level')=='紧急' for x in rows),
            sum(x.get('level')=='截止时间待核实' for x in rows)))
    for key,label in (('onlineSync','查看发货在线明细'),('shortageSync','查看缺货配件明细')):
        sync=report.get(key,{})
        if sync.get('complete') and str(sync.get('url','')).startswith('https://'):
            lines.append('[%s](%s)' % (label,sync['url']))
        elif sync.get('enabled'):
            lines.append(label+'：本轮同步未确认，请查看工作台。')
    if report.get('errors'):lines.append('查询异常 %s 项，详见工作台。' % len(report['errors']))
    lines.append('检查时间：'+str(report.get('checkedAt','')).replace('T',' ')[:19])
    return '\n\n'.join(lines)


def message(report, notification=False):
    if report.get('error'):
        return '【紧急发货监控】\n查询失败：' + report['error']
    if notification:
        return notification_summary(report)
    # 群消息聚焦真正紧急单；截止时间缺失及查询异常汇总提示，完整明细留在软件中。
    orders = [x for x in report['orders'] if not notification or x['level'] != '截止时间待核实']
    errors = report['errors'][:5] if notification else report['errors']
    online = report.get('onlineSync', {})
    sync_lines = []
    if online.get('enabled'):
        sync_lines = [f"在线汇总：更新 {online.get('updated',0)} 单，确认揽收后删除 {online.get('deleted',0)} 单。" +
                      ('同步有异常，请查看运行记录。' if not online.get('complete') else ''), online.get('url','')]
    # 缺货表单独报告同步失败，避免订单表成功掩盖配件表异常。
    shortage = report.get('shortageSync', {})
    if shortage.get('enabled'):
        sync_lines.append(f"缺货汇总：{shortage.get('shortageCount',0)} 种配件缺货，更新 {shortage.get('updated',0)} 行。" +
                          ('同步有异常，请查看运行记录。' if not shortage.get('complete') else ''))
    return '\n\n'.join(['【紧急发货监控】',
        '检查时间：' + report['checkedAt'][:16].replace('T', ' ') + '（北京时间）',
        f"待发扫描 {report['scanned']} 单｜48小时内/超时 {sum(x['level'] in ('紧急','已超时') for x in report['orders'])} 单｜截止待核实 {sum(x['level']=='截止时间待核实' for x in report['orders'])} 单",
        report['stockBasis'], *sync_lines, *[order_text(x, compact=notification) for x in orders],
        *([f"查询异常 {len(report['errors'])} 项：\n" + '\n'.join(errors) + ('\n完整异常及截止待核实订单见软件“紧急发货”页。' if notification else '')] if report['errors'] else [])])


def deliver(job_id, report, root, runner=subprocess.run):
    """逐段保存发送回执；未知投递不重试，未变化提醒保持静默。"""
    from group_order_bot import queue_db, DWS
    target = config(root)['target']
    # 不包含动态倒计时，但包含紧急升级、负责人和库存结果，避免每轮重复提醒。
    stable = {k: v for k, v in report.items() if k not in ('jobId', 'checkedAt')}
    stable['missingDeadlineCount'] = sum(x['level'] == '截止时间待核实' for x in report.get('orders', []))
    stable['orders'] = [{k: v for k, v in x.items() if k != 'hours'} for x in report.get('orders', []) if x['level'] != '截止时间待核实']
    signature = hashlib.sha256(json.dumps([target, stable], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with contextlib.closing(queue_db(root)) as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['delivery'] != 'pending':
            return
        previous = db.execute("SELECT value FROM settings WHERE key='shippingNotification'").fetchone()
        if job['origin'] == 'schedule' and previous and previous[0] == signature:
            db.execute("UPDATE jobs SET delivery='suppressed' WHERE id=?", (job_id,))
            return
        text = message(report, notification=True)
        # 按字符切段，保留所有订单；每段控制在钉钉文本大小范围内。
        chunks = [text[i:i+1500] for i in range(0, len(text), 1500)]
        if db.execute("UPDATE jobs SET delivery='unknown' WHERE id=? AND delivery='pending'", (job_id,)).rowcount != 1:
            return
        receipts = []
        try:
            for index, chunk in enumerate(chunks, 1):
                result = runner([DWS, 'chat', '+messages-send', '--as', 'bot', '--robot-code', target['robotCode'],
                    '--groups', target['groupId'], '--markdown', chunk,
                    '--profile', target['profile'], '--format', 'json', '--yes'],
                    capture_output=True, text=True, encoding='utf-8', timeout=45,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                receipt = json.loads(result.stdout)
                receipts.append(receipt)
                db.execute('UPDATE jobs SET evidence=? WHERE id=?', (json.dumps(receipts, ensure_ascii=False), job_id))
                if result.returncode != 0 or receipt.get('succeededCount') != 1 or receipt.get('failedCount') != 0:
                    return
            db.execute("UPDATE jobs SET delivery='sent' WHERE id=?", (job_id,))
            db.execute("INSERT OR REPLACE INTO settings VALUES('shippingNotification',?)", (signature,))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # unknown 保留给人工核验；不重复发送可能已收到的分段。
            return
