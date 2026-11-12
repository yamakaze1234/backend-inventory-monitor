"""连接与通知设置中的运营指令群绑定。"""
import hashlib
import json
import re
from pathlib import Path

from ops_command_api import checked_id
from ops_commands import COMMANDS, GroupCommands
from ops_permission_import import LIMIT, inspect, member_rights, write_template

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
    # 编辑群功能不能覆盖成员授权，尤其不能覆盖刚确认导入的权限。
    return GroupCommands(service).save(*identity, commands, name=name.strip())


def identity_for(values):
    identity = tuple(values.get(k) for k in ('org_id', 'robot_id', 'group_id'))
    if not all(map(checked_id, identity)):
        raise ValueError('请从已绑定的群打开成员权限')
    return identity


def state_for(service, db, identity):
    commands = GroupCommands.selected(db, *identity)
    if commands is None:
        raise ValueError('群未绑定，请先保存群功能')
    key = GroupCommands.member_key(*identity)
    return dict(name=service.get(db, key + ':name', ''), commands=commands, members=service.get(db, key, {}))


def stamp(state):
    # 回执绑定所见的群功能和成员清单，避免旧预览覆盖并发保存。
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def current(service, identity):
    GroupCommands(service)
    with service.db() as db:
        return state_for(service, db, identity)


def available_shops(b):
    from order_bot_config import load
    from price_maintenance import saved_sources
    from inventory_shop_names import shop_names
    configured = {str(s['id']) for s in load(b.service.root).get('shops', [])}
    if getattr(b, 'douyin', None) and getattr(b.douyin, 'store', None):
        configured.update(str(s['shop_id']) for s in saved_sources(b.douyin.store))
    names = shop_names(b.service.root)
    return {key: names.get(key, key) for key in configured}


def commit_members(service, identity, members, expected):
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        state = state_for(service, db, identity)
        if stamp(state) != expected:
            raise ValueError('群或成员权限已经变化，请重新打开权限设置或重新预览导入')
        service.put(db, GroupCommands.member_key(*identity), members)
        if state_for(service, db, identity)['members'] != members:
            raise RuntimeError('成员权限回读不一致')
    return len(members)


def save_members(b, values):
    identity = identity_for(values)
    rows = values.get('members')
    if not isinstance(rows, list) or len(rows) > LIMIT or any(not isinstance(r, dict) for r in rows):
        raise ValueError('成员清单无效，最多 200 人')
    members, allowed = {}, available_shops(b)
    for row in rows:
        user, rights = member_rights(row, split, allowed)
        if user in members:
            raise ValueError('成员 ID 重复')
        members[user] = rights
    return commit_members(b.service, identity, members, values.get('expected'))


def template(b, values):
    identity = identity_for(values)
    state = current(b.service, identity)
    path = Path(b.service.root) / 'console_exports' / ('ops-' + stamp(identity)[:20]) / '运营机器人权限导入模板.xlsx'
    path.parent.mkdir(parents=True, exist_ok=True)
    write_template(path, state['commands'], available_shops(b))
    return dict(message='权限模板已生成。填写后回到同一个群选择导入。', download=b.review('download', path), filename=path.name)


def preview_import(b, values):
    from inventory_business_views import T, A
    identity = identity_for(values)
    state = current(b.service, identity)
    shops = available_shops(b)
    members, rows, errors = inspect(values.get('path'), state['members'], split, shops)
    tables = []
    if rows:
        tables.append(T('成员权限变更预览', [('line','表格行'), ('user_id','钉钉成员 ID'), ('operation','处理'),
                                             ('commands','指令 ID'), ('shops','店铺 ID'), ('fields','附加权限 ID')], rows))
    if errors:
        tables.append(T('待修正的表格行', [('line','表格行'), ('reason','原因')], errors))
        return dict(message=f'发现 {len(errors)} 处问题；修正后重新导入。本次未保存。', tables=tables)
    added = sum(row['operation'] == '新增' for row in rows)
    changed = sum(row['operation'] == '变更' for row in rows)
    name = state['name'] or identity[2]
    forms = []
    if added or changed:
        token = b.review('ops-members-import', (identity, members, stamp(state), frozenset(shops)))
        forms.append(A('ops-members-import-confirm', '确认导入权限', values=dict(token=token),
                       confirm=f'群：{name}。新增 {added} 人，更新 {changed} 人；表格未列出的成员保持原权限。'))
    return dict(message=f'{name}：新增 {added} 人，变更 {changed} 人，无变化 {len(rows)-added-changed} 人。尚未保存。',
                tables=tables, forms=forms)


def confirm_import(b, values):
    identity, members, expected, shops = b.reviewed(values.get('token'), 'ops-members-import')
    if shops != available_shops(b).keys():
        raise ValueError('当前工作区的店铺范围已变化，请重新预览导入')
    return commit_members(b.service, identity, members, expected)


def binding_form(b, identity, name, *, robot_name='', token=None, commands=None):
    from inventory_business_views import F, A

    if commands is None:
        # 已绑定群重新识别时沿用勾选；尚未绑定才使用当前可用功能的默认值。
        commands = GroupCommands(b.service).get(*identity_for(identity))
    if commands is None:
        api = getattr(b, 'ops_command_api', None)
        commands = (getattr(api.business, 'available', api.business.implemented) if api else
                    {'help', 'inventory.get', 'price.changes', 'monitor.add', 'monitor.threshold.set'})
    values = dict(identity, name=name)
    if token:
        values['token'] = token
    values.update({key: [k for k in choices if k in commands] for key, (_, choices) in SECTIONS.items()})
    fields = [F('name', '群名称', readonly=True),
              *([F('robot_name','机器人名称',readonly=True)] if robot_name else []),
              F('org_id', '钉钉组织 ID', readonly=True),
              F('robot_id', '机器人 ID', readonly=True), F('group_id', '钉钉群 ID', readonly=True),
              *[F(key, label, 'multi', options=[(k, COMMANDS[k][0]) for k in choices], bulk=True)
                for key, (label, choices) in SECTIONS.items()]]
    if robot_name:
        values['robot_name'] = robot_name
    return A('ops-group-save', '确认群绑定' if token else '编辑群功能', fields, values, module='settings',
             description='群和机器人标识已核实。请核对名称并选择要开放的功能，保存后生效。',
             submitLabel='保存群设置')


def save_binding(b, values):
    identity = identity_for(values)
    token = values.get('token')
    if token:
        target = b.reviewed(token, 'ops-group-verified')
        if identity != identity_for(target):
            raise ValueError('群或机器人与识别结果不一致，请重新识别')
        name = target['name']
    else:
        # 本机已有绑定可以编辑功能；新群必须走真实识别流程。
        name = current(b.service, identity)['name']
    return save(b.service, dict(values, name=name))


def section(b):
    from inventory_business_views import F, A, T, S
    api = getattr(b, 'ops_command_api', None)
    groups = GroupCommands(b.service)
    rows = []
    with b.service.db() as db:
        for org, robot, group, commands in db.execute('SELECT * FROM ops_command_groups ORDER BY org_id,robot_id,group_id'):
            selected = json.loads(commands)
            key = groups.member_key(org, robot, group)
            grants = b.service.get(db, key, {})
            members = [dict(user_id=user, **{k: ','.join(v) for k, v in member.items()}) for user, member in grants.items()]
            identity = dict(org_id=org, robot_id=robot, group_id=group)
            row = dict(identity, name=b.service.get(db, key + ':name', ''), commands=selected)
            member_fields = [F('members', '已核实的授权成员', 'rows', columns=[
                ('user_id', '钉钉成员 ID'), ('commands', '指令 ID（逗号分隔）'),
                ('shops', '店铺 ID（逗号分隔）'), ('fields', '附加权限 ID（逗号分隔）')])]
            expected = stamp(dict(name=row['name'], commands=selected, members=grants))
            rows.append(dict(id=json.dumps([org, robot, group]), name=row['name'], **identity, member_count=len(members),
                             features='、'.join(COMMANDS[k][0] for k in selected),
                             _forms=[binding_form(b, identity, row['name'], commands=selected),
                                     A('ops-members-save', '编辑成员权限', member_fields,
                                       dict(identity, members=members, expected=expected),
                                       confirm='按当前清单保存。删除的成员会失去该群的指令权限。'),
                                     A('ops-members-template', '下载权限模板', values=identity, immediate=True),
                                     A('ops-members-import-preview', '导入权限表格',
                                       [F('path','成员权限表格（.xlsx）','file')], identity)]))
    result = S('ops-commands', '运营机器人指令')
    status = ('接口已启用。' if api else '运营指令接入配置无效，请核对本机配置。' if
              getattr(b, 'ops_command_config_error', False) else '接口尚未接入运营机器人。')
    result['description'] = status + '按已核实的群身份保存功能；成员还需单独授权。'
    result['text'] = ('指令 ID：' + '、'.join(f'{label} {key}' for key, (label, _) in COMMANDS.items())
                      + '。附加权限：ERP 成本 erp_cost、维护表价格 maintenance_price、含税（核算）成本 tax_cost、操作人 operator、表格入口 table_links。')
    result['forms'] = [A('ops-group-discover', '绑定钉钉群', [F('name','群名称')], submitLabel='识别群信息',
                         description='填写完整钉钉群名，识别后会自动填入组织、机器人和群 ID。')]
    result['tables'] = [T('群功能配置', [('name', '群名称'), ('org_id', '组织'), ('robot_id', '机器人'),
                                     ('group_id', '群 ID'), ('features', '已勾选功能'), ('member_count','授权成员')], rows)]
    from ops_tax_cost_settings import panel as cost_panel
    cost_forms,cost_tables = cost_panel(b)
    result['forms'].extend(cost_forms)
    result['tables'].extend(cost_tables)
    return result
