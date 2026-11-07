"""适合聊天窄屏的紧凑库存通知；完整证据仍保留在本地事件中。"""
from collections import Counter
from datetime import datetime, timezone, timedelta
from html import escape

LABELS = dict(negative_stock='负库存', oversold='超售', out_of_stock='库存归零预警',
              low='低库存', inbound='确认入库', stock_increase='库存增加',
              pending_inbound='待入库提醒', recovery='风险恢复')
ORDER = dict(negative_stock=0, oversold=0, out_of_stock=1, low=2,
             recovery=3, inbound=4, stock_increase=5, pending_inbound=6)
# 只给风险、数量和辅助信息着色，商品名称沿用客户端默认文字色。
STYLES = dict(negative_stock='#C62828', oversold='#C62828',
              out_of_stock='#C62828', low='#AD5700',
              recovery='#16834A', inbound='#1769AA',
              stock_increase='#1769AA', pending_inbound='#7450A3')
MUTED = '#6B7280'


def colored(text, color):
    # 使用钉钉 Markdown 的字体标签，只保留文字颜色，不添加彩色圆点。
    return f'<font color="{color}">{text}</font>'


def stamp(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') if value else '时间未知'


def plain(value):
    # 商品名称中的 Markdown 符号不能变成标题、链接或额外的换行。
    text = escape(' '.join(str(value or '').split()), quote=False)
    for char in ('\\', '*', '_', '[', ']', '`', '#'):
        text = text.replace(char, '\\' + char)
    return text


def number(value):
    return '未知' if value is None else f'{value:g}' if isinstance(value, (int, float)) else plain(value)


def transition(previous, current):
    delta = f'（{current-previous:+g}）' if isinstance(previous, (int, float)) and isinstance(current, (int, float)) else ''
    return f'{number(previous)} → {number(current)}{delta}'


def event_change(event):
    """表格与消息共用变化口径，库存增加始终使用实际库存差值。"""
    current, previous = event.get('after') or {}, event.get('before') or {}
    special = current.get('stock_basis') == 'warehouse_stock'
    kind = event['kind']
    if kind == 'stock_increase':
        key, label = ('warehouse_stock', '分库库存') if special else ('stock', '实际库存')
    elif kind == 'pending_inbound':
        key, label = ('warehouse_purchase', '分库待入') if special else ('purchase', '待入')
        if current.get('pending_source') == 'erp_total':
            key, label = 'purchase', 'ERP 总待入'
    else:
        key, label = ('monitor_qty', '分库库存') if special else ('able', '可销')
    return f'{label} {transition(previous.get(key), current.get(key))}'


def compact_events(events, service, now):
    if not events:
        return '暂无库存提醒。'
    counts = Counter(e['kind'] for e in events)
    times = sorted({e.get('observed_at', 0) for e in events})
    collected = stamp(times[0]) if len(times) == 1 else f'{stamp(times[0])} ～ {stamp(times[-1])}'
    products = {p['sku']: p for p in service.products()}
    summary = []
    for kind in sorted(counts, key=lambda k: ORDER.get(k, 9)):
        color = STYLES.get(kind, MUTED)
        # 恢复后仍低于预警线时，摘要也保留橙色，避免误读为库存充足。
        if kind == 'recovery' and any(
                e['kind'] == kind and
                (qty := (e.get('after') or {}).get('monitor_qty' if (e.get('after') or {}).get('stock_basis') == 'warehouse_stock' else 'able')) is not None and
                qty <= e['threshold'] for e in events):
            color = STYLES['low']
        summary.append(colored(f'{LABELS.get(kind,kind)} {counts[kind]}', color))
    parts = ['### 库存提醒 · ' + ' / '.join(summary),
             colored('采集时间：' + collected, MUTED)]
    heading, notes = None, set()
    for event in sorted(events, key=lambda e: ORDER.get(e['kind'], 9)):
        current, previous = event.get('after') or {}, event.get('before') or {}
        kind, special = event['kind'], current.get('stock_basis') == 'warehouse_stock'
        quantity = current.get('monitor_qty' if special else 'able')
        label = LABELS.get(kind, kind)
        color = STYLES.get(kind, MUTED)
        if kind == 'recovery' and quantity is not None and quantity <= event['threshold']:
            label = '负库存解除，仍低库存' if special else '超售解除，仍低库存'
            color = STYLES['low']
        if heading != label:
            parts.append(f'**{colored(label, color)}**')
            heading = label
        gid = current.get('goods_id') or event.get('goods_id') or products.get(event['sku'], {}).get('goods_id')
        title = f"**{plain(event['name'])}**" + ('\n' + colored(f'商品编号：{plain(gid)}', MUTED) if gid else '')
        depot = current.get('warehouse_name') or event.get('warehouse') or '公司大库'
        if current.get('pending_source') == 'erp_total':
            detail = colored(f"ERP 总待入：{number(current.get('purchase'))} 件", color)
            detail += colored(f" · 仓库未确认；上次 {number(previous.get('purchase'))}", MUTED)
            notes.add('总待入不代表所选仓库待入，也不代表已入库。')
        else:
            detail = '**' + colored(event_change(event), color) + '**' + colored(f' · {plain(depot)}', MUTED)
            if kind == 'stock_increase' and not special:
                detail += f"\n可销 {transition(previous.get('able'), current.get('able'))}"
            if kind in ('low', 'oversold', 'negative_stock', 'out_of_stock', 'recovery'):
                detail += colored(f" · 预警值 {number(event['threshold'])}", MUTED)
            if special and kind == 'recovery':
                detail += f" · 上次分库库存：{number(previous.get('monitor_qty'))}"
        if kind == 'stock_increase':
            notes.add('库存增加不等同于确认入库；本轮依据实际库存快照变化。')
        if kind == 'out_of_stock':
            notes.add('分库库存已降至 0，请核对受影响配置和商品链接。' if special else '可销库存已降至 0，请核对受影响配置和商品链接。')
        inbound = current.get('inbound')
        if kind == 'inbound' and isinstance(inbound, dict):
            detail += f"\n入库单：{plain(inbound.get('bill_code'))} · {plain(inbound.get('kind'))} · 入库数量：{number(inbound.get('in_qty'))} · {stamp(inbound.get('created_at'))}"
        if event.get('cause') == 'threshold_changed':
            detail += '\n触发原因：预警值调整'
        messages = service.related_messages(event['sku'], now=now)
        if messages:
            hours = max(0, now - max(m['created_at'] for m in messages)) / 3600
            detail += f'\n另有 {len(messages)} 条货源消息，最近 {hours:.1f} 小时前；全文见提醒记录。'
        parts.append(title + '\n' + detail)
    parts.extend(sorted(notes))
    # Markdown 硬换行确保同一条商品内部换行；商品之间只有一个段落间距。
    return '\n\n'.join(parts).replace('\n', '  \n')
