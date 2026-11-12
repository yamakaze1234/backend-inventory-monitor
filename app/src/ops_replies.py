"""仅用结构化回执渲染群回复，不发送消息。"""
from datetime import datetime

from ops_commands import COMMANDS
from ops_sources import CN, NUMBER_TYPES, stamp


def when(value):
    at = stamp(value)
    return datetime.fromtimestamp(at, CN).strftime('%m-%d %H:%M') if at is not None else '时间待核实'


def money(value):
    if type(value) is not int:
        return '未知'
    amount = abs(value)
    return f'{"-" if value < 0 else ""}¥{amount // 100:,}.{amount % 100:02}'


def qty(value, unit='件'):
    return f'{value} {unit}' if value is not None else '未知'


def warehouse(data):
    return (data.get('warehouse_name') or '公司大库') + ('实物库存' if data.get('stock_basis') == 'warehouse_stock' else '实际可销')


def inventory(data):
    if data.get('outcome') == 'needs_clarification':
        return ['【库存｜请确认型号】请补充显存、品牌或 ERP ID。',
                *(f"{r['name']}｜ERP ID {r['goods_id']}" for r in data['candidates'])]
    items, total = data['items'], data['total']
    lines = ['【库存｜关键词查询结果】' if data.get('keyword') else
             '【库存｜数据未知】' if all(r.get('qty') is None for r in items) else '【库存｜查询结果】']
    if data.get('keyword'):
        lines.append(f"{data['keyword']} · 匹配 {total} 件")
    if data.get('interpreted_terms'):
        lines.append('按「' + '、'.join(data['interpreted_terms']) + '」查询')
    for row in items:
        value = warehouse(row) + ' ' + qty(row.get('qty'))
        if row.get('erp_able') is not None and row.get('pending_review') is not None:
            value += f"（ERP 可销 {qty(row['erp_able'])}，待审 {qty(row['pending_review'])}）"
        lines += [f"{row['name']}｜ERP ID {row['goods_id']}", value]
        if row.get('observed_at') is not None:
            lines.append(('历史采集时间 ' if row.get('stock_status') == 'historical' else '采集时间 ') + when(row['observed_at']))
        if row.get('erp_cost_status') != 'forbidden':
            amount = row.get('erp_unit_cost_cents')
            lines.append('ERP 库存成本：未知' if amount is None else
                         f"ERP 库存成本 {money(amount)}/件（公司大库，读取于 {when(row.get('erp_cost_observed_at'))}）")
        tax_state = row.get('tax_cost_status')
        if tax_state == 'ready':
            lines.append(f"含税（核算）成本 {money(row['tax_cost_cents'])}/件（{row['tax_cost_source']}，保存于 {when(row['tax_cost_observed_at'])}）")
        elif tax_state and tax_state != 'forbidden':
            note = {'not_bound':'未连接成本表','read_failed':'成本表读取失败','no_match':'成本表无此 ERP 商品',
                    'conflict':'同一 ERP 商品有不同成本，待核实','unknown':'成本单元格为空或未保存公式结果'}.get(tax_state,'未知')
            lines.append('含税（核算）成本：' + note)
        for price in row.get('maintenance_prices', []):
            unit = price['price_kind'] == 'part_unit_price'
            lines.append(f"主机维护表{'配件单价' if unit else '整机到手价'} {money(price['price_cents'])}{'/件' if unit else ''}"
                         f"（{price.get('shop_name', price['shop_id'])} · {price['config_name']}）")
        status = {'not_bound':'未绑定维护表', 'no_match':'未找到关联配置', 'column_unverified':'价格列未核实',
                  'read_failed':'读取失败', 'conflict':'价格或配置身份待核实'}.get(row.get('maintenance_price_status'))
        if status:
            lines.append('主机维护表价格：' + status)
        if row.get('maintenance_price_total', 0) > 3:
            lines.append(f"共 {row['maintenance_price_total']} 条维护表价格，可指定店铺缩小范围。")
        lines.append('')
    if total > 5:
        lines.append(f'共匹配 {total} 件，本次显示 5 件，可补充关键词或按 ERP ID 查询。')
    return lines


def price_changes(data):
    if data.get('outcome') == 'no_batch':
        return ['【成本变化｜暂无批次】暂无已完成的成本检测批次。']
    lines = ['【成本变化｜查询结果】', '检测时间 ' + when(data.get('checked_at'))]
    for row in data['items']:
        change = ('+' if row['delta_cents'] > 0 else '') + money(row['delta_cents'])
        lines += [f"{row['name']}（ERP {row['goods_id']}）· {row['warehouse']}",
                  f"{money(row['before_cents'])} → {money(row['after_cents'])}（{change}）"]
    if not data['items']:
        lines.append('本批次未发现符合条件的成本变化。')
    if data['total'] > len(data['items']):
        lines.append(f"共 {data['total']} 条，本次显示 {len(data['items'])} 条，可按 ERP 商品 ID 查询。")
    return lines


def monitor(command, data):
    label = COMMANDS[command][0]
    result = {'created':'已保存','updated':'已保存','already_exists':'已关注','unchanged':'已是目标值'}[data['outcome']]
    lines = [f'【{label}｜{result}】', f"{data['name']}｜ERP ID {data['goods_id']}"]
    if result == '已关注':
        lines.append(f"{warehouse(data)}｜当前阈值 {qty(data['threshold'])}。可发送「修改监控阈值 {data['goods_id']} 为 {data['requested_threshold']}」。")
    elif command == 'monitor.threshold.set' and result == '已保存':
        lines.append(f"{warehouse(data)}｜低库存阈值 {qty(data['previous_threshold'])} → {qty(data['threshold'])}")
    else:
        lines.append(f"{warehouse(data)}｜低库存阈值 {qty(data['threshold'])}")
    if data['outcome'] == 'created':
        names = dict(inbound='入库', pending_inbound='待入库', low='低库存', oversold='超售', negative_stock='负库存', recovery='恢复')
        lines += ['监控已启用，提醒项：' + '、'.join(names[k] for k, v in data['alerts'].items() if v and k in names),
                  '库存将在后续采集后显示。']
    if data.get('enabled') is False:
        lines.append('当前监控已停用。')
    return lines


def order_events(data):
    if data['outcome'] == 'no_records':
        return ['【订单状态｜暂无记录】已找到关联订单，暂无可展示的业务记录。']
    label = NUMBER_TYPES[data['number_type']] if data['number_type'] != 'erp_no' else 'ERP NO 单号'
    lines = ['【订单状态｜查询结果】', f"{label}：{data['number']}"]
    if data['order_count'] > 1:
        lines.append(f"关联 {data['order_count']} 张订单")
    latest = data['latest_events']
    lines.append(('关联订单最近记录：' if data['order_count'] > 1 else '最近记录：') +
                 '；'.join(f"{r['step']}（{when(r['occurred_at'])}）" for r in latest) if latest else '最近状态待核实')
    lines.append('订单记录')
    for event in data['events']:
        lines.append('　'.join(str(v) for v in (when(event['occurred_at']), event['step'], event.get('reference_number'), event.get('operator')) if v))
    return lines


def tasks(reply, progress=False):
    status, data = reply['status'], reply['data']
    title = '订单巡检' if reply['command'] == 'order.patrol' else '订单匹配'
    label, note = {
        'queued': ('已入队', '任务已排队，等待执行。' if progress else '执行结束后回复结果。'),
        'running': ('执行中', '任务正在执行，可用回执查询进度。'),
        'completed': ('已完成', '任务已结束，可核对回执。'),
        'needs_review': ('待核对', '部分结果尚待核对，请查看工作台任务记录。'),
        'unknown': ('结果待核对', '执行结果尚未确认，请按原回执查询，暂勿重复提交。'),
        'failed': ('执行失败', '请在工作台核对任务记录。'),
    }[status]
    if status in ('completed', 'needs_review') and 'success' in data:
        if status == 'needs_review' and data['success']:
            label = '部分完成'
        note = f"已匹配 {qty(data['success'], data['unit'])}，已有有效关联跳过 {qty(data['skipped'], data['unit'])}。"
        if status == 'needs_review':
            note += f"{qty(data['review'], data['unit'])}待核对。" if data['review'] else '扫描结果仍待核对。'
            if data.get('reason'):
                note += '原因：' + data['reason'] + '。'
        elif title == '订单匹配' and data['unit'] == '单' and data['success'] == 1:
            note = '已匹配并回读确认。'
        elif title == '订单匹配' and data['unit'] == '单' and data['skipped'] == 1:
            note = '已有有效关联，已跳过。'
        elif title == '订单巡检' and not (data['success'] or data['skipped'] or data['review']):
            note = '本次未发现待匹配订单。'
    target = data.get('shop_name') or data.get('shop_id')
    number = data.get('order_number')
    if number and target:
        target += f" · {'ERP 单号' if number.upper().startswith('NO-') else '网店单号'} {number}"
    if data.get('duplicate') and status in ('queued', 'running'):
        note = '已复用现有任务，可用本回执查询进度。'
    lines = [f'【{title}｜{label}】', target or '', note]
    if progress:
        state = {'sent':'已投递','pending':'待发送','failed':'投递失败','unknown':'投递状态待核对','suppressed':'由原任务负责'}.get(reply.get('delivery_status'))
        if state:
            lines.append('最终回复：' + state)
    return lines


def shipping(data):
    result = data['outcome']
    if result == 'not_bound':
        return ['【发货监控｜表格未绑定】请联系管理员配置发货监控 AI 表格。']
    scope = '、'.join(data.get('shop_names') or data['shop_ids'])
    if result == 'no_report':
        return [f'【发货监控｜暂无汇总】钉钉 AI 表格暂无 {scope} 的已核实汇总。']
    if result == 'ready' and data['counts']['pending_scanned'] == 0:
        title = '无待发订单'
    else:
        title = '在线汇总' if result == 'ready' else '部分数据'
    lines = [f'【发货监控｜{title}】', '范围：' + scope]
    if data.get('source_note'):
        lines.append(data['source_note'])
    labels = dict(pending_scanned='待发扫描', overdue='已超时', due_within_48h='48 小时内到期',
                  deadline_unknown='截止待核实', shortage_goods='缺货配件', inventory_unknown_goods='库存待核实')
    for report in data['tables']:
        orders = report['source'] == 'shipping_orders'
        label = '发货订单表' if orders else '缺货配件表'
        state = {'not_bound':'未绑定','forbidden':'无权限','read_failed':'读取失败','partial':'部分数据',
                 'unknown':'同步待核实','failed':'同步失败','no_report':'暂无报告'}.get(report['state'])
        lines.append(label + (' · ' + when(report['checked_at']) if report.get('checked_at') is not None else '') + (' · ' + state if state else ''))
        if report.get('batch_id'):
            if report.get('sync_state') in ('failed', 'unknown'):
                lines.append('本轮同步失败' if report['sync_state'] == 'failed' else '本轮同步待核实')
            lines.append('｜'.join(labels[k] + ' ' + qty(v, '单' if orders else '种') for k, v in report['counts'].items() if v is not None or not data.get('source_note')))
        for item in report.get('shortage_items', []):
            lines.append(f"{item['name']}（ERP {item['goods_id']}）缺口 {qty(item['shortage'])}｜待入 {qty(item['incoming'])}")
        if report.get('table_url'):
            lines.append(('点击表格查看明细：' if orders else '缺货配件：') + report['table_url'])
        if orders and report.get('views'):
            lines.append('岗位视图：' + '｜'.join(f'{k} {v}' for k, v in report['views'].items()))
    return lines


def front(data):
    if data['outcome'] == 'not_bound':
        return ['【前台库存｜表格未绑定】请联系管理员配置前台库存 AI 表格。']
    if data['outcome'] == 'no_report':
        lines = ['【前台库存｜暂无监控记录】该店铺或 SKU 暂无在线监控记录。']
        # 没有命中记录时，也允许有权限的成员打开对应明细表。
        if data.get('table_url'):
            lines.append('前台库存监控明细表：' + data['table_url'])
        return lines
    lines = ['【前台库存｜部分数据】以下为已回读核实的监控记录。' if data['outcome'] == 'partial' else '【前台库存｜在线监控明细】',
             f"{data.get('shop_name', data['shop_id'])} · {'最近快照' if data.get('detail_source') else '批次'} {when(data.get('checked_at'))}",
             f"{'表内监控记录' if data.get('detail_source') else '待处理'} {data['counts']['total']} 个 SKU"]
    lines[1] += ''.join(f"｜{label} {data[key]}" for key, label in (('spu','SPU'),('sku_id','SKU')) if data.get(key))
    for row in data['items']:
        lines.append(f"{row.get('product_name') or row['spu']} · {row.get('config_name') or row['sku_id']}｜平台库存 {qty(row['platform_stock'])}｜"
                     + '｜'.join(str(row.get(k) or '待核实') for k in ('parts_state', 'replenish_state', 'handling_state')))
        lines.append(f"平台快照 {when(row.get('snapshot_at'))}｜配件核对 {when(row.get('parts_checked_at'))}")
    if data['counts']['total'] > data['counts']['shown']:
        lines.append(f"本次显示 {data['counts']['shown']} 条，可按 SPU 或 SKU 查询。")
    if data.get('table_url'):
        lines.append('前台库存监控明细表：' + data['table_url'])
    return lines


# 文案按指令 ID 维护，最终只渲染回执中已通过权限检查的项目。
HELP_GUIDE = {
    'inventory.get': (
        '查询最近一次库存快照。默认看公司大库实际可销；指定分库时看该库实物库存。',
        '按 ERP 商品 ID：查询商品 56281',
        '按名称、型号或 SKU：查询商品 5070 Ti 雪鹰',
        '也可以说：查询 5070 Ti 雪鹰库存和 ERP 价格',
        '可选：在末尾加「仓库 <仓库ID>」指定分库，加「店铺 <店铺名或ID>」限定维护表价格范围。两项都用时先写仓库，再写店铺。',
        '回复会标注采集时间；库存 0 表示确实为零，「未知」表示没有可用数据。价格按权限展示；已连接主机成本表时，有权限的成员会另外看到含税（核算）成本。',
    ),
    'price.changes': (
        '查看最近一次已完成的检测里，ERP 库存成本有哪些变化；回复包含调整前后金额、差额和检测时间。',
        '查全部变化：查价格变化',
        '只查某商品：查价格变化 56281',
        '每次最多显示 10 条；未发现变化与尚无完成批次会分别提示。',
    ),
    'monitor.add': (
        '将指定 ERP 商品加入库存监控，设置低库存阈值。若已关注，会返回现有阈值。',
        '示例：添加监控 56281 阈值 5',
        '分库监控可在末尾加「仓库 <仓库ID>」。',
    ),
    'monitor.threshold.set': (
        '修改已关注商品的低库存阈值，阈值可设为 0。',
        '示例：修改监控阈值 56281 为 10',
    ),
    'order.status.get': (
        '按完整订单号查看关联订单和最近业务记录；网店单号的 A、A-1 后缀需保留。',
        '示例：查订单状态 123456789012345A-1',
        '也可直接填写 NO- 或 CK- 单号；物流单号在前面加「物流」。可在末尾加「店铺 <店铺名或ID>」。',
    ),
    'order.match': (
        '将指定店铺的订单加入匹配队列，随后根据回执查看执行结果。',
        '示例：匹配订单 店铺A 123456789012345A-1',
    ),
    'order.patrol': (
        '将指定店铺的订单巡检加入队列。',
        '示例：巡检订单 店铺A',
    ),
    'task.status': (
        '查看自己在本群发起的订单任务进度，填写机器人回复中的完整回执编号。',
        '格式：查看任务进度 <回执编号>',
    ),
    'shipping.summary': (
        '查看最近一次已回读的发货统计，包括超时、即将到期和缺货。',
        '查已授权范围：发货监控汇总',
        '只查一家店：发货监控汇总 店铺A',
    ),
    'front_stock.summary': (
        '查看指定店铺最近的前台库存监控结果和待处理 SKU。',
        '示例：前台库存监控 店铺A',
        '可在末尾加「SPU <商品ID>」或「SKU <SKU ID>」进一步筛选。',
    ),
}


def help_lines(data):
    choices = [choice for choice in data['commands'] if choice['id'] != 'help']
    lines = ['【运营机器人｜帮助】']
    if not choices:
        return lines + ['本群暂无可用业务指令，请联系管理员检查群功能设置。']
    lines.append(f'以下是你在本群可用的 {len(choices)} 项业务指令。发送时先 @机器人，再输入指令和参数。')
    for index, choice in enumerate(choices, 1):
        # 接收端与工作台可独立更新；本地命令名称优先于旧工作台的显示名。
        label = COMMANDS.get(choice['id'], (choice['label'], ''))[0]
        lines.extend(('', f'{index}. {label}'))
        lines.extend(HELP_GUIDE.get(choice['id'], (f"示例：{choice['example']}",)))
    return lines


def render(reply):
    command, code, data = reply['command'], reply['code'], reply['data']
    label = COMMANDS.get(command, ('指令', ''))[0]
    receipt = reply.get('request_id')
    attach = command in ('monitor.add', 'monitor.threshold.set', 'order.match', 'order.patrol', 'task.status')
    if code != 'OK':
        titles = dict(FORBIDDEN='权限不足', COMMAND_DISABLED='本群未启用', IDEMPOTENCY_CONFLICT='回执冲突',
                      INVALID_ARGUMENT='请补全参数', AMBIGUOUS_TARGET='请确认对象', NOT_FOUND='未找到',
                      SERVICE_UNAVAILABLE='暂不可用', SOURCE_UNAVAILABLE='暂不可用')
        if command == 'monitor.threshold.set' and code == 'NOT_FOUND':
            lines = ['【修改监控阈值｜未关注】请先添加该商品的库存监控。']
        elif command == 'inventory.get' and code == 'NOT_FOUND':
            lines = ['【库存｜未找到】未找到匹配商品，请补充名称、型号、SKU 或完整 ERP ID。']
        else:
            lines = [f"【{label}｜{titles.get(code, '处理失败')}】{reply['message']}"]
        attach |= code in ('SERVICE_UNAVAILABLE','SOURCE_UNAVAILABLE','IDEMPOTENCY_CONFLICT')
    elif command == 'help':
        lines = help_lines(data)
    elif command == 'inventory.get':
        lines = inventory(data)
    elif command == 'price.changes':
        lines = price_changes(data)
    elif command in ('monitor.add', 'monitor.threshold.set'):
        lines = monitor(command, data)
    elif command == 'order.status.get':
        lines = order_events(data)
    elif command == 'shipping.summary':
        lines = shipping(data)
    elif command == 'front_stock.summary':
        lines = front(data)
    elif command == 'task.status':
        lines = tasks(data['task'], progress=True)
        receipt = data['task']['request_id']
    else:
        lines = tasks(reply)
    if attach and receipt:
        lines.append('回执：' + receipt)
    return '\n'.join(lines).strip()
