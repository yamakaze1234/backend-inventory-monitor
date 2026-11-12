"""运营机器人指令的隔离解析和群功能配置；不接收事件或执行外部操作。"""
import json
import re
import unicodedata

# 目录同时提供显示名称和示例，帮助与群开关共用稳定指令 ID。
COMMANDS = {
    'help': ('帮助', '帮助'),
    'inventory.get': ('查询商品', '查询商品 56281'),
    'price.changes': ('查价格变化', '查价格变化 56281'),
    'monitor.add': ('添加监控', '添加监控 56281 阈值 5'),
    'monitor.threshold.set': ('修改监控阈值', '修改监控阈值 56281 为 10'),
    'order.status.get': ('查订单状态', '查订单状态 123456789012345A-1'),
    'order.match': ('匹配订单', '匹配订单 店铺A 123456789012345A-1'),
    'order.patrol': ('巡检订单', '巡检订单 店铺A'),
    'task.status': ('查看任务进度', '查看任务进度 dingtalk:示例'),
    'shipping.summary': ('发货监控汇总', '发货监控汇总'),
    'front_stock.summary': ('前台库存监控', '前台库存监控 店铺A'),
}


# 仅匹配明确要求查库存或 ERP 成本的短句；不会扩展到下单、改价或订单查询。
ERP_COST_WORD = r'ERP\s*(?:库存成本|成本价|价格|单价|成本)'
NATURAL_PREFIX = re.compile(r'(?:查询|帮我查(?:询|一下)?|查一下|查)\s*')
NATURAL_INVENTORY = re.compile(
    r'(.+?)\s*(?:的\s*)?'
    rf'(?:库存(?:\s*(?:和|及|与|、|以及|/)\s*{ERP_COST_WORD})?|{ERP_COST_WORD})[?。]?',
    re.IGNORECASE,
)


def inventory_args(value, **scope):
    numeric = unicodedata.normalize('NFKC', value)
    if numeric.isascii() and numeric.isdecimal():
        return dict(scope, goods_id=numeric)
    return dict(scope, keyword=value)


def parse_command(text):
    """只解析明确的固定指令；调用者必须先验证真实 @ 事件和机器人身份。"""
    if not isinstance(text, str) or len(text) > 500:
        raise ValueError('指令无效')
    text = ' '.join(text.split())
    if text == '帮助':
        return 'help', {}
    m = re.fullmatch(r'(?:查询商品|查库存) (.+?)(?: 仓库 (\S+))?(?: 店铺 (.+))?', text)
    if m:
        value = m[1]
        scope = {k: v for k, v in zip(('warehouse_id', 'shop_alias'), m.groups()[1:]) if v}
        return 'inventory.get', inventory_args(value, **scope)
    # 识别用户常用的「查询某型号库存和 ERP 价格」，再走同一个只读指令。
    natural = unicodedata.normalize('NFKC', text)
    prefix = NATURAL_PREFIX.match(natural)
    m = NATURAL_INVENTORY.fullmatch(natural[prefix.end():]) if prefix else None
    if m and (value := m[1].strip()) and not any(mark in value for mark in (';', '。', '?', '!')):
        return 'inventory.get', inventory_args(value)
    m = re.fullmatch(r'查价格变化(?: ([0-9]+))?', text)
    if m:
        return 'price.changes', ({'goods_id': m[1]} if m[1] else {})
    m = re.fullmatch(r'添加监控 ([0-9]+) 阈值 ([0-9]+)(?: 仓库 (\S+))?', text)
    if m:
        scope = dict(stock_basis='warehouse_stock', warehouse_id=m[3]) if m[3] else {}
        return 'monitor.add', dict(scope, goods_id=m[1], threshold=_threshold(m[2]))
    m = re.fullmatch(r'修改监控阈值 ([0-9]+) 为 ([0-9]+)', text)
    if m:
        return 'monitor.threshold.set', {'goods_id': m[1], 'threshold': _threshold(m[2])}
    m = re.fullmatch(r'查订单状态 (物流 )?(\S+)(?: 店铺 (.+))?', text)
    if m:
        number = m[2]
        kind = 'tracking_no' if m[1] else 'erp_no' if number.upper().startswith('NO-') else 'outbound_no' if number.upper().startswith('CK-') else 'web_order_no'
        # 显式物流类型不能与 ERP 或出库单前缀冲突。
        if m[1] and number.upper().startswith(('NO-', 'CK-')):
            raise ValueError('单号类型与单号冲突')
        if not m[1] and number == '物流':
            raise ValueError('请提供完整物流单号')
        return 'order.status.get', {'number': number, 'number_type': kind, **({'shop_alias': m[3]} if m[3] else {})}
    m = re.fullmatch(r'匹配订单 (.+) (\S+)', text)
    if m:
        return 'order.match', {'shop_alias': m[1], 'order_number': m[2]}
    m = re.fullmatch(r'前台库存监控 (.+?)(?: SPU (\S+))?(?: SKU (\S+))?', text, re.I)
    if m:
        return 'front_stock.summary', {k: v for k, v in zip(('shop_alias', 'spu', 'sku_id'), m.groups()) if v}
    for pattern, command, key in ((r'巡检订单 (.+)', 'order.patrol', 'shop_alias'),
                                   (r'查看任务进度 (\S+)', 'task.status', 'request_id')):
        m = re.fullmatch(pattern, text)
        if m:
            return command, {key: m[1]}
    m = re.fullmatch(r'发货监控汇总(?: (.+))?', text)
    if m:
        return 'shipping.summary', ({'shop_alias': m[1]} if m[1] else {})
    raise ValueError('未识别指令或参数不完整')


def _threshold(value):
    # 严格保留零并限制业务允许的整数范围。
    result = int(value)
    if result > 100_000_000:
        raise ValueError('阈值超出范围')
    return result


class GroupCommands:
    """使用库存工作区数据库保存明确绑定的组织、机器人和群。"""
    def __init__(self, service):
        self.service = service
        with service.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ops_command_groups (org_id TEXT NOT NULL, robot_id TEXT NOT NULL, group_id TEXT NOT NULL, commands TEXT NOT NULL, PRIMARY KEY (org_id, robot_id, group_id))')

    @staticmethod
    def member_key(org_id, robot_id, group_id):
        return 'ops_members:' + json.dumps([org_id, robot_id, group_id], ensure_ascii=False)

    @staticmethod
    def selected(db, org_id, robot_id, group_id):
        row = db.execute('SELECT commands FROM ops_command_groups WHERE org_id=? AND robot_id=? AND group_id=?',
                         (org_id, robot_id, group_id)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, org_id, robot_id, group_id, commands=None, members=None, name=None):
        # 新绑定默认全选；空列表表示管理员主动清空，不等同于默认值。
        if any(not isinstance(x, str) or not x.strip() for x in (org_id, robot_id, group_id)):
            raise ValueError('群身份不完整')
        with self.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = self.selected(db, org_id, robot_id, group_id)
            # 只有首次绑定默认全选，保存空列表表示明确清空。
            selected = (existing if existing is not None else list(COMMANDS)) if commands is None else commands
            if not isinstance(selected, list) or any(not isinstance(x, str) or x not in COMMANDS for x in selected) or len(set(selected)) != len(selected):
                raise ValueError('功能选择无效')
            db.execute('INSERT OR REPLACE INTO ops_command_groups VALUES (?,?,?,?)', (org_id, robot_id, group_id, json.dumps(selected)))
            key = self.member_key(org_id, robot_id, group_id)
            if members is not None:
                self.service.put(db, key, members)
            if name is not None:
                self.service.put(db, key + ':name', name)
            return self.selected(db, org_id, robot_id, group_id)

    def get(self, org_id, robot_id, group_id):
        with self.service.db() as db:
            row = db.execute('SELECT commands FROM ops_command_groups WHERE org_id=? AND robot_id=? AND group_id=?', (org_id, robot_id, group_id)).fetchone()
        return json.loads(row[0]) if row else None

    def available(self, org_id, robot_id, group_id, permitted, implemented):
        # 未绑定群关闭；可见功能还须同时满足成员授权和实际实现状态。
        selected = self.get(org_id, robot_id, group_id)
        if selected is None:
            return []
        return [command for command in COMMANDS if command in selected and command in permitted and command in implemented]


def render_help(available):
    """帮助只展示同时通过群配置、成员权限和实现状态检查的功能。"""
    if 'help' not in available:
        raise ValueError('本群未启用帮助或成员无权使用')
    entries = [COMMANDS[key][1] for key in COMMANDS if key != 'help' and key in available]
    if not entries:
        return '【运营机器人｜帮助】本群暂无可用业务指令，请联系管理员检查群功能设置'
    return '【运营机器人｜帮助】\n' + '\n'.join(entries)
