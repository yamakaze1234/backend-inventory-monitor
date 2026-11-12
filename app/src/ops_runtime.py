"""接到现有库存实例；外部源只有在工作区明确绑定后启用。"""
import json
import os
from pathlib import Path

from ops_command_api import CommandAPI, CommandError, denied
from ops_inventory import InventoryCommands, normalized
from ops_sources import ReportTables, order_status


class CommandBusiness:
    def __init__(self, inventory, *, order=None, order_read=None, reports=None, shops=None):
        self.inventory, self.order, self.order_read = inventory, order, order_read
        self.reports, self.shops = reports or ReportTables(), shops or (lambda: {})
        self.implemented = {'help', 'inventory.get', 'price.changes', 'monitor.add', 'monitor.threshold.set'}
        if order is not None:
            self.implemented.update(('order.match', 'order.patrol', 'task.status'))
        if order_read is not None:
            self.implemented.add('order.status.get')
        self.available = set(self.implemented)
        self.implemented.update(('shipping.summary', 'front_stock.summary'))
        # 功能有处理器但尚未绑定时，直接请求可说明原因；帮助仅列有来源的查询。
        if {'shipping_orders', 'shipping_shortages'} & self.reports.bindings.keys():
            self.available.add('shipping.summary')
        if 'front_stock' in self.reports.bindings:
            self.available.add('front_stock.summary')

    def _shop(self, value, access):
        if value in access.shops:
            return value
        # 只在成员授权范围内匹配别名；重名时不选择第一项。
        names = self.shops()
        found = [key for key in access.shops if key in names and normalized(names[key]) == normalized(value)]
        if len(found) > 1:
            raise CommandError(409, 'AMBIGUOUS_TARGET', '店铺别名不唯一，请提供店铺编号')
        if not found:
            raise denied()
        return found[0]

    def execute(self, command, args, access, external_id, db=None):
        if 'shop_id' in args:
            args = dict(args, shop_id=self._shop(args['shop_id'], access))
        if command == 'inventory.get':
            return self.inventory.inventory(args, access)
        if command == 'price.changes':
            return self.inventory.price_changes(args)
        if command in ('monitor.add', 'monitor.threshold.set'):
            return self.inventory.monitor(command, args, db)
        if command == 'order.status.get':
            return order_status(self.order_read, args, access)
        if command == 'front_stock.summary':
            result = self.reports.front(args['shop_id'], args)
            result['shop_name'] = self.shops().get(args['shop_id'], args['shop_id'])
            return result
        if command == 'shipping.summary':
            scope = {args['shop_id']} if 'shop_id' in args else access.shops
            if not scope:
                raise denied()
            result = self.reports.shipping(scope)
            names = self.shops()
            result['shop_names'] = [names.get(k, k) for k in result['shop_ids']]
            return result
        if command in ('order.match', 'order.patrol'):
            shop = args['shop_id']
            state = self.order.snapshot()
            if (not state.get('workerOnline') or not state.get('serviceEnabled') or state.get('stopRequested')
                    or shop not in {s['id'] for s in state.get('shops', [])}):
                raise CommandError(503, 'SERVICE_UNAVAILABLE', '订单服务未启动或店铺未配置')
            number = args.get('order_number', '')
            if command == 'order.match':
                import re
                if not re.fullmatch(r'[1-9][0-9]{14,24}(?:A(?:-[0-9]+)?)?|NO-[A-Za-z0-9-]{4,70}', number, re.I):
                    raise CommandError(400, 'INVALID_ARGUMENT', '请提供完整网店单号或 ERP NO 单号')
            value = self.order.action('ops-enqueue', dict(kind='order' if command == 'order.match' else 'today',
                                                          number=number, shop=shop, external_id=external_id))
            if value.get('status') == 'unavailable':
                raise CommandError(503, 'SERVICE_UNAVAILABLE', '订单服务当前不可用')
            status = value.get('status')
            if not value.get('jobId') or status not in ('queued', 'running', 'unknown', 'completed', 'failed', 'needs_review'):
                status = 'unknown'
            return dict(_status=status, task_id=value.get('jobId'), shop_id=shop, order_number=number,
                        shop_name=self.shops().get(shop, shop), duplicate=bool(value.get('duplicate')))
        raise CommandError(503, 'SERVICE_UNAVAILABLE', '该指令的数据来源尚未接入')

    def task(self, external_id):
        return self.order.ops_task(external_id) if self.order else None


def pilot_connection(root):
    """读取本机部署的回环连接；凭证仅在当前 Windows 用户的进程内解密。"""
    from ops_command_api import checked_id
    environment = tuple(os.environ.get('INVENTORY_OPS_' + key) for key in ('TOKEN', 'ROBOT_ID', 'ORG_ID'))
    if all(environment):
        return (*environment, int(os.environ.get('INVENTORY_OPS_PORT', '0')))
    path = Path(root) / 'ops-command-pilot.json'
    if not path.is_file():
        return None
    values = json.loads(path.read_text(encoding='utf-8-sig'))
    robot, org, port = (values.get(k) for k in ('robot_id', 'org_id', 'port'))
    if not checked_id(robot) or not checked_id(org) or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('本机运营指令连接配置无效')
    from monitor_secret_store import unprotect
    protected = path.with_name('ops-command-key.dat')
    if not 0 < protected.stat().st_size <= 16384:
        raise ValueError('本机运营指令凭证文件无效')
    token = unprotect(protected.read_bytes()).decode('ascii')
    return token, robot, org, port


def configured_api(service, order, business=None):
    # 没有同时配置专用令牌和机器人标识时，正式程序与预览都不会开放指令。
    if getattr(service, 'offline_preview', False) or getattr(business, 'offline', False):
        return None
    connection = pilot_connection(service.root)
    if connection is None:
        return None
    token, robot, org, port = connection
    from erp_price_monitor import CoordinatedProfitSource
    from inventory_ai import call, config
    from inventory_shop_names import shop_names
    from price_maintenance import saved_sources

    path = Path(service.root) / 'ops-command-sources.json'
    binding = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    maintenance = binding.get('maintenance_prices')
    if maintenance is None:
        maintenance = saved_sources(business.douyin.store) if business and business.douyin else []
    ai = None
    if binding.get('inventory_ai') is True and config(service).get('model'):
        def ai(keyword):
            return json.loads(call(service, [
                dict(role='system', content='只解析商品搜索词。输出 JSON 对象，candidates 最多三个搜索词，conditions 为原话明确的规格。不要生成 ERP ID、库存或价格。'),
                dict(role='user', content=keyword),
            ], timeout=8))
    tables = binding.get('report_tables')
    if tables:
        required = {'scope', 'batch', 'checked_at', 'sync_state', 'payload'}
        if len({item['profile'] for item in tables.values()}) != 1 or any(
                set(item['fields']) != required or len(set(item['fields'].values())) != len(required)
                for item in tables.values()):
            raise ValueError('运营指令批次表绑定无效')
    order_read = None
    if binding.get('order_status_bridge') is True and order.ops_supported():
        order_read = order.ops_order_status
    inventory = InventoryCommands(service, cost=CoordinatedProfitSource(getattr(service, 'sql_worker', None)).read_profit_costs,
                                  ai=ai, maintenance=maintenance, aliases=binding.get('inventory_aliases'))
    api = CommandAPI(service, CommandBusiness(inventory, order=order if order.ops_supported() else None,
                                             order_read=order_read, reports=ReportTables(tables) if tables else None,
                                             shops=lambda: shop_names(service.root)), robot, token, org_id=org)
    api.local_port = port
    return api
