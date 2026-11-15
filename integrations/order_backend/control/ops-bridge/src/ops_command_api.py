"""工作区本机指令服务：授权、幂等回执和业务入口。"""
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from urllib.parse import unquote

from ops_commands import COMMANDS, GroupCommands

PREFIX = '/api/commands/v1/'
WRITES = frozenset(('monitor.add', 'monitor.threshold.set', 'order.match', 'order.patrol'))
PRICE_ACCESS = frozenset(('erp_cost', 'maintenance_price', 'tax_cost', 'operator', 'table_links'))
ARGS = {
    'help': ({}, {}),
    'inventory.get': ({}, dict(goods_id='goods', keyword='text', warehouse_id='id', shop_id='id')),
    'price.changes': ({}, dict(goods_id='goods')),
    'monitor.add': (dict(goods_id='goods', threshold='threshold'), dict(stock_basis='basis', warehouse_id='id')),
    'monitor.threshold.set': (dict(goods_id='goods', threshold='threshold'), {}),
    'order.status.get': (dict(number='id'), dict(number_type='number_type', shop_id='id')),
    'order.match': (dict(shop_id='id', order_number='id'), {}),
    'order.patrol': (dict(shop_id='id'), {}),
    'task.status': (dict(request_id='receipt'), {}),
    'shipping.summary': ({}, dict(shop_id='id')),
    'front_stock.summary': (dict(shop_id='id'), dict(spu='id', sku_id='id')),
}
ACTOR = ('org_id', 'group_id', 'user_id')
TERMINAL = frozenset(('completed', 'needs_review', 'failed'))
ORDER_TASKS = frozenset(('order.match','order.patrol','task.status'))
ID = re.compile(r'[^\s\x00-\x1f\x7f]{1,200}\Z')
RECEIPT = re.compile(r'dingtalk:[A-Za-z0-9_.:-]{1,150}\Z')


class CommandError(ValueError):
    def __init__(self, http, code, message, data=None):
        super().__init__(message)
        self.http, self.code, self.data = http, code, data or {}


def bad(message):
    return CommandError(400, 'INVALID_ARGUMENT', message)


def denied():
    return CommandError(403, 'FORBIDDEN', '当前群或账号无权使用此指令')


def envelope(request_id, command, status='completed', code='OK', message='', **data):
    return dict(request_id=request_id, command=command, status=status, code=code, message=message, data=data)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def checked_id(value):
    return isinstance(value, str) and bool(ID.fullmatch(value))


def checked_actor(value):
    if not isinstance(value, dict) or set(value) != set(ACTOR) or not all(map(checked_id, value.values())):
        raise bad('需要完整组织、群及成员标识')
    return tuple(value[k] for k in ACTOR)


def validated_args(command, value):
    required, optional = ARGS[command]
    if not isinstance(value, dict) or set(required) - set(value) or set(value) - (required | optional).keys():
        raise bad('缺少参数或包含不支持的参数：' + '、'.join(required))
    for key, entry in value.items():
        kind = (required | optional)[key]
        valid = (type(entry) is int and 0 <= entry <= 100_000_000) if kind == 'threshold' else (
            isinstance(entry, str) and {
                'id': lambda: checked_id(entry) or (key == 'shop_id' and 0 < len(entry) <= 100 and entry.isprintable() and entry == entry.strip()),
                'goods': lambda: bool(re.fullmatch('[1-9][0-9]{0,19}', entry)),
                'text': lambda: 0 < len(entry.strip()) <= 150 and entry.isprintable(),
                'basis': lambda: entry in ('company_able', 'warehouse_stock'),
                'number_type': lambda: entry in ('web_order_no', 'erp_no', 'outbound_no', 'tracking_no'),
                'receipt': lambda: bool(RECEIPT.fullmatch(entry)),
            }[kind]())
        if not valid:
            raise bad('请核对参数：' + key)
    if command == 'inventory.get' and ('goods_id' in value) == ('keyword' in value):
        raise bad('ERP 编号和关键词请选择一项')
    if command == 'monitor.add' and (value.get('stock_basis') == 'warehouse_stock') != ('warehouse_id' in value):
        raise bad('指定分库时须同时提供分库口径和仓库 ID')
    return value


@dataclass(frozen=True)
class Access:
    shops: frozenset
    fields: frozenset
    commands: tuple


class CommandAPI:
    def __init__(self, service, business, robot_id, token, *, org_id=None):
        if not checked_id(robot_id) or not isinstance(token, str) or not token.isascii() or len(token) < 32:
            raise ValueError('运营机器人指令凭证配置无效')
        if org_id is not None and not checked_id(org_id):
            raise ValueError('运营机器人组织标识无效')
        self.service, self.business, self.robot_id, self.token = service, business, robot_id, token
        self.org_id = org_id
        self.groups = GroupCommands(service)
        with service.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ops_requests (
                robot TEXT NOT NULL, org TEXT NOT NULL, id TEXT NOT NULL, actor TEXT NOT NULL,
                command TEXT NOT NULL, fingerprint TEXT NOT NULL, http INTEGER NOT NULL, response TEXT NOT NULL,
                PRIMARY KEY (robot, org, id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS ops_replies (
                robot TEXT NOT NULL, org TEXT NOT NULL, id TEXT NOT NULL, phase TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(robot, org, id, phase))''')

    def _access(self, db, actor, command, *, existing=False):
        org, group, user = actor
        if self.org_id is not None and org != self.org_id:
            raise denied()
        selected = self.groups.selected(db, org, self.robot_id, group)
        member = self.service.get(db, self.groups.member_key(org, self.robot_id, group), {}).get(user)
        if selected is None or not member:
            raise denied()
        if command not in selected and not existing:
            raise CommandError(403, 'COMMAND_DISABLED', '本群未启用此功能', {'help_available': 'help' in selected and 'help' in member['commands']})
        if command not in member['commands']:
            raise denied()
        target = getattr(self.business,'order_target',None)
        # 订单写入仅在已核实可投递最终结果的群启用。
        implemented = self.business.implemented
        if target and (group!=target['groupId'] or (command in ORDER_TASKS or command=='help')
                       and not self.business.order_route_current()):
            implemented = implemented - ORDER_TASKS
        if command not in implemented:
            raise CommandError(503, 'SERVICE_UNAVAILABLE', '该指令的数据来源尚未接入')
        return Access(frozenset(member['shops']), frozenset(member['fields']),
                      tuple(k for k in COMMANDS if k in selected and k in member['commands'] and k in implemented))

    def _key(self, actor, ident):
        return self.robot_id, actor[0], ident

    def _row(self, db, actor, ident):
        row = db.execute('SELECT actor,command,fingerprint,http,response FROM ops_requests WHERE robot=? AND org=? AND id=?',
                         self._key(actor, ident)).fetchone()
        if row and row[0] != canonical(actor):
            raise denied()
        return row

    def _store(self, db, actor, ident, http, reply):
        db.execute('UPDATE ops_requests SET http=?,response=? WHERE robot=? AND org=? AND id=?',
                   (http, canonical(reply), *self._key(actor, ident)))

    def _project(self, reply, access):
        # 重放也按当前权限投影，撤销价格或链接权限立即生效。
        data = reply['data']
        if not set(data.get('shop_ids', ())) <= access.shops or (data.get('shop_id') and data['shop_id'] not in access.shops):
            raise denied()
        for item in data.get('items', ()):
            if 'erp_cost' not in access.fields and 'erp_unit_cost_cents' in item:
                item.update(erp_unit_cost_cents=None, erp_cost_observed_at=None, erp_cost_status='forbidden')
            if 'tax_cost' not in access.fields and 'tax_cost_status' in item:
                item.update(tax_cost_status='forbidden',tax_cost_cents=None)
                item.pop('tax_cost_source',None)
                item.pop('tax_cost_observed_at',None)
            if 'maintenance_prices' in item:
                if 'maintenance_price' not in access.fields:
                    item.update(maintenance_prices=[], maintenance_price_total=0, maintenance_price_status='forbidden')
                elif not set(item.pop('_maintenance_shops', ())) <= access.shops:
                    item.update(maintenance_prices=[], maintenance_price_total=0, maintenance_price_status='forbidden')
                item.pop('_maintenance_shops', None)
        if 'operator' not in access.fields:
            for event in data.get('events', []) + data.get('latest_events', []):
                event['operator'] = None
        # 单店查询也可能指向共享表；链接权限必须覆盖整张表，重放时同样复核。
        link_shops = set(data.pop('_table_link_shops', ()))
        if not link_shops <= access.shops:
            data.pop('table_url', None)
        if 'table_links' not in access.fields:
            data.pop('table_url', None)
            for report in data.get('tables', []):
                report.pop('table_url', None)
                report.pop('views', None)
        if reply['command'] == 'price.changes' and 'erp_cost' not in access.fields:
            raise denied()
        if reply['command'] == 'help':
            data['commands'] = self._help(access)
        # 报告中嵌入的任务状态也可能因权限撤销而失效。
        if reply['command'] == 'task.status' and 'task' in data:
            task_shop = data['task']['data'].get('shop_id')
            if task_shop and task_shop not in access.shops:
                raise denied()
        return reply

    def _help(self, access):
        available = getattr(self.business, 'available', self.business.implemented)
        return [dict(id=k, label=COMMANDS[k][0], example=COMMANDS[k][1]) for k in access.commands
                if k in available and (k != 'price.changes' or 'erp_cost' in access.fields)]

    def _run(self, command, args, access, external_id, db=None):
        if command == 'help':
            return dict(commands=self._help(access))
        if command == 'price.changes' and 'erp_cost' not in access.fields:
            raise denied()
        return self.business.execute(command, args, access, external_id, db)

    def submit(self, body):
        if set(body) != {'request_id', 'command', 'actor', 'args'}:
            raise bad('请求字段不完整或不支持')
        ident, command = body['request_id'], body['command']
        if not isinstance(ident, str) or not RECEIPT.fullmatch(ident) or not isinstance(command, str) or command not in ARGS:
            raise bad('回执编号或指令无效')
        actor = checked_actor(body['actor'])
        args = validated_args(command, body['args'])
        fingerprint = hashlib.sha256(canonical((command, args)).encode()).hexdigest()
        # 两种库存写操作和回执共用一个 SQLite 事务；外部动作先落未知占位，再调用一次。
        local_write = command in ('monitor.add', 'monitor.threshold.set')
        with self.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            access = self._access(db, actor, command)
            row = self._row(db, actor, ident)
            if row:
                if row[2] != fingerprint:
                    raise CommandError(409, 'IDEMPOTENCY_CONFLICT', '回执编号对应不同参数')
                replay = self._project(json.loads(row[4]), access)
                if command == 'task.status' and 'task' in replay['data']:
                    self._access(db, actor, replay['data']['task']['command'], existing=True)
                replay['replayed'] = True
                return row[3], replay
            pending = envelope(ident, command, 'unknown', 'OK', '结果待核对', _pending=True)
            db.execute('INSERT INTO ops_requests VALUES (?,?,?,?,?,?,?,?)',
                       (*self._key(actor, ident), canonical(actor), command, fingerprint, 200, canonical(pending)))
            if local_write:
                db.execute('SAVEPOINT business_write')
                http, reply = self._execute(ident, command, args, access, actor, db)
                if http != 200:
                    db.execute('ROLLBACK TO business_write')
                db.execute('RELEASE business_write')
                self._store(db, actor, ident, http, reply)
                return http, self._project(reply, access)
        if command == 'task.status':
            http, reply = self._execute_status(actor, args['request_id'], ident)
        else:
            http, reply = self._execute(ident, command, args, access, actor)
        with self.service.db() as db:
            self._store(db, actor, ident, http, reply)
        with self.service.db() as db:
            return http, self._project(reply, self._access(db, actor, command))

    def _execute(self, ident, command, args, access, actor, db=None):
        try:
            data = self._run(command, args, access, self.external_id(actor, ident), db)
            status = data.pop('_status', 'completed')
            reply = envelope(ident, command, status, 'OK', '', **data)
            if 'task_id' in data:
                reply['task_id'] = data.pop('task_id')
            return (202 if status == 'queued' else 200), reply
        except CommandError as error:
            return error.http, envelope(ident, command, 'failed', error.code, str(error), **error.data)
        except Exception:
            if db is not None:
                # 未预期异常必须回滚业务写入和回执占位。
                raise
            if command in ('order.match', 'order.patrol'):
                return 200, envelope(ident, command, 'unknown', 'OK', '结果待核对')
            return 503, envelope(ident, command, 'failed', 'SOURCE_UNAVAILABLE', '数据源暂不可读')

    def external_id(self, actor, ident):
        return hashlib.sha256(canonical((self.robot_id, actor, ident)).encode()).hexdigest()

    def _execute_status(self, actor, target, ident):
        try:
            result = self.status(actor, target)
            return 200, envelope(ident, 'task.status', status=result['status'], code=result['code'],
                                 message=result['message'], task=result)
        except CommandError as error:
            return error.http, envelope(ident, 'task.status', 'failed', error.code, str(error))

    def status(self, actor, ident):
        with self.service.db() as db:
            self._access(db, actor, 'task.status')
            row = self._row(db, actor, ident)
            if not row or row[1] not in ('order.match', 'order.patrol'):
                raise CommandError(404, 'NOT_FOUND', '未找到此任务')
            access = self._access(db, actor, row[1], existing=True)
            result = self._project(json.loads(row[4]), access)
        refreshed = self._refresh_task(actor, ident, result, access)
        with self.service.db() as db:
            self._access(db, actor, 'task.status')
            return self._project(refreshed, self._access(db, actor, row[1], existing=True))

    def _refresh_task(self, actor, ident, result, access):
        # 查询只按持久外部请求编号回读；任何回读失败都保留已知状态。
        if result['status'] in ('queued', 'running', 'unknown', *TERMINAL) and result['code'] == 'OK':
            try:
                task = self.business.task(self.external_id(actor, ident))
            except Exception:
                task = None
            if task:
                if task['shop_id'] not in access.shops:
                    raise denied()
                result['status'] = task['status']
                result['task_id'] = task['task_id']
                result['data'].update({k: v for k, v in task.items() if k not in ('status', 'task_id', 'created_at')})
        with self.service.db() as db:
            row = db.execute("SELECT state FROM ops_replies WHERE robot=? AND org=? AND id=? AND phase='final'",
                             self._key(actor, ident)).fetchone()
        delivery = ('unknown' if row[0] == 'sending' else row[0]) if row else 'pending'
        if result['data'].get('final_reply_owned') is False:
            delivery = 'suppressed'
        return dict(result, delivery_status=delivery)

    def delivery(self, actor, ident, body, *, internal=False):
        phase, state = body.get('phase'), body.get('state')
        if set(body) != {'phase', 'state'} or phase not in ('initial', 'final') or state not in ('claim', 'sent', 'failed', 'unknown'):
            raise bad('回复状态无效')
        # 已绑定路由的最终回复必须经过桌面端的切换锁，HTTP 接收器只处理首条回复。
        if phase=='final' and not internal and getattr(self.business,'current_order_target',None):
            raise denied()
        with self.service.db() as db:
            row = self._row(db, actor, ident)
            if not row:
                raise CommandError(404, 'NOT_FOUND', '未找到回执')
            access = self._access(db, actor, row[1], existing=phase == 'final')
            reply = self._project(json.loads(row[4]), access)
        if phase == 'final':
            if row[1] not in ('order.match', 'order.patrol'):
                raise bad('该指令没有最终回复')
            final = self._refresh_task(actor, ident, reply, access)
            if final['data'].get('final_reply_owned') is False or reply['code'] != 'OK':
                return {'claimed': False, 'done': True}
            if final['status'] not in (*TERMINAL, 'unknown') or final['data'].get('final_reply_owned') is not True:
                raise CommandError(409, 'NOT_READY', '任务尚未结束或由原任务回复')
        elif reply['data'].get('_pending'):
            raise CommandError(409, 'NOT_READY', '请求结果仍待核对')
        key = (*self._key(actor, ident), phase)
        with self.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._access(db, actor, row[1], existing=phase == 'final')
            if state == 'claim':
                cursor = db.execute("INSERT OR IGNORE INTO ops_replies VALUES (?,?,?,?,'sending')", key)
                result = {'claimed': cursor.rowcount == 1, 'done': cursor.rowcount != 1}
                if cursor.rowcount == 1 and phase == 'final':
                    result['reply'] = final
                return result
            cursor = db.execute("UPDATE ops_replies SET state=? WHERE robot=? AND org=? AND id=? AND phase=? AND state='sending'",
                                (state, *key))
            return {'recorded': cursor.rowcount == 1}

    def route(self, method, path, headers, body=None):
        ident, command = '', ''
        try:
            provided = headers.get('Authorization', '')
            if not provided.isascii() or not secrets.compare_digest(provided, 'Bearer ' + self.token):
                raise denied()
            suffix = path.removeprefix(PREFIX)
            if method == 'GET' and suffix == 'capabilities':
                return 200, dict(version='commands/v1', commands={k: dict(required=ARGS[k][0], optional=ARGS[k][1])
                                 for k in COMMANDS if k in self.business.implemented})
            if method == 'GET' and suffix == 'member':
                # 接收端只凭可信回调身份探测授权成员；不会产生业务回执或执行指令。
                org, group, user = checked_actor({key:headers.get('X-Actor-' + key.replace('_','-').title()) for key in ACTOR})
                if self.org_id is not None and org != self.org_id:
                    raise denied()
                with self.service.db() as db:
                    enabled = self.groups.selected(db,org,self.robot_id,group)
                    member = self.service.get(db,self.groups.member_key(org,self.robot_id,group),{}).get(user)
                    if enabled is None or not member:
                        raise denied()
                return 200, {'member':True}
            if method == 'POST' and suffix == 'requests':
                if not isinstance(body, dict):
                    raise bad('请求必须是对象')
                ident = body.get('request_id') if isinstance(body.get('request_id'), str) else ''
                command = body.get('command') if isinstance(body.get('command'), str) else ''
                return self.submit(body)
            if suffix.startswith('requests/'):
                _, _, tail = suffix.partition('/')
                encoded, _, action = tail.partition('/')
                ident = unquote(encoded)
                if not RECEIPT.fullmatch(ident):
                    raise bad('回执编号无效')
                actor = checked_actor({key: headers.get('X-Actor-' + key.replace('_', '-').title()) for key in ACTOR})
                if method == 'GET' and not action:
                    command = 'task.status'
                    return 200, self.status(actor, ident)
                if method == 'POST' and action == 'delivery' and isinstance(body, dict):
                    return 200, self.delivery(actor, ident, body)
            raise CommandError(404, 'NOT_FOUND', '接口不存在')
        except CommandError as error:
            return error.http, envelope(ident, command, 'failed', error.code, str(error), **error.data)
        except Exception:
            return 503, envelope(ident, command, 'failed', 'SERVICE_UNAVAILABLE', '指令服务暂不可用')
