"""连接与通知设置中的运营指令群绑定。"""
import json
import re

from ops_command_api import PRICE_ACCESS, checked_id
from ops_commands import COMMANDS, GroupCommands

SECTIONS = {
    'help': ('帮助', ('help',)),
    'inventory': ('库存与价格', ('inventory.get', 'price.changes')),
    'monitor': ('监控设置', ('monitor.add', 'monitor.threshold.set')),
    'orders': ('订单与发货', ('order.status.get', 'order.match', 'order.patrol', 'task.status', 'shipping.summary', 'front_stock.summary')),
}


def split(value):
    return list(dict.fromkeys(part for part in re.split(r'[\s,，]+', str(value or '').strip()) if part))


def save(service, values):
    identity = [values.get(k) for k in ('org_id', 'robot_id', 'group_id')]
    name = values.get('name', '')
    if not all(map(checked_id, identity)) or not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('请填写已核实的群名、组织 ID、机器人 ID 及群 ID')
    selections = [values.get(key, []) for key in SECTIONS]
    if any(not isinstance(row, list) or any(k not in group[1] for k in row) for row, group in zip(selections, SECTIONS.values())):
        raise ValueError('功能选择无效')
    commands = [key for row in selections for key in row]
    members = {}
    rows = values.get('members', [])
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError('成员清单无效，最多 200 人')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('成员清单无效')
        member = row.get('user_id')
        if not checked_id(member) or member in members:
            raise ValueError('成员 ID 不完整或重复')
        rights, shops, fields = (split(row.get(key)) for key in ('commands', 'shops', 'fields'))
        if not set(rights) <= COMMANDS.keys() or not set(fields) <= PRICE_ACCESS or not all(map(checked_id, shops)):
            raise ValueError('成员的指令、店铺编号或附加数据权限无效')
        members[member] = dict(commands=rights, shops=shops, fields=fields)
    groups = GroupCommands(service)
    selected = groups.save(*identity, commands, members=members, name=name.strip())
    return selected


def section(b):
    from inventory_business_views import F, A, T, S
    api = getattr(b, 'ops_command_api', None)
    default = getattr(api.business, 'available', api.business.implemented) if api else {'help', 'inventory.get', 'price.changes', 'monitor.add', 'monitor.threshold.set'}

    def form(row=None):
        values = dict(row or {})
        keys = values.pop('commands', default)
        values.update({key: [k for k in choices if k in keys] for key, (_, choices) in SECTIONS.items()})
        fields = [F('name', '群名称'), F('org_id', '钉钉组织 ID', readonly=bool(row)),
                  F('robot_id', '机器人 ID', readonly=bool(row)), F('group_id', '钉钉群 ID', readonly=bool(row)),
                  *[F(key, label, 'multi', options=[(k, COMMANDS[k][0]) for k in choices], bulk=True)
                    for key, (label, choices) in SECTIONS.items()],
                  F('members', '已核实的授权成员', 'rows', columns=[
                      ('user_id', '成员 ID'), ('commands', '允许的指令 ID（逗号分隔）'),
                      ('shops', '允许的店铺 ID（逗号分隔）'), ('fields', '附加权限 ID（逗号分隔）')])]
        return A('ops-group-save', '编辑群功能和成员' if row else '绑定已核实的群', fields, values)

    groups = GroupCommands(b.service)
    rows = []
    with b.service.db() as db:
        for org, robot, group, commands in db.execute('SELECT * FROM ops_command_groups ORDER BY org_id,robot_id,group_id'):
            selected = json.loads(commands)
            key = groups.member_key(org, robot, group)
            members = [dict(user_id=user, **{k: ','.join(v) for k, v in member.items()})
                       for user, member in b.service.get(db, key, {}).items()]
            row = dict(name=b.service.get(db, key + ':name', ''), org_id=org, robot_id=robot, group_id=group,
                       commands=selected, members=members)
            rows.append(dict(id=json.dumps([org, robot, group]), name=row['name'], org_id=org, robot_id=robot,
                             group_id=group, features='、'.join(COMMANDS[k][0] for k in selected),
                             _forms=[form(row)]))
    result = S('ops-commands', '运营机器人指令')
    status = ('接口已启用。' if api else '运营指令接入配置无效，请核对本机配置。' if
              getattr(b, 'ops_command_config_error', False) else '接口尚未接入运营机器人。')
    result['description'] = status + '按已核实的群身份保存功能；成员还需单独授权。'
    result['text'] = ('指令 ID：' + '、'.join(f'{label} {key}' for key, (label, _) in COMMANDS.items())
                      + '。附加权限：ERP 成本 erp_cost、维护表价格 maintenance_price、操作人 operator、表格入口 table_links。')
    result['forms'] = [form()]
    result['tables'] = [T('群功能配置', [('name', '群名称'), ('org_id', '组织'), ('robot_id', '机器人'),
                                     ('group_id', '群 ID'), ('features', '已勾选功能')], rows)]
    return result
