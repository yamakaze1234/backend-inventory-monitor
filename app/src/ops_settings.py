"""连接与通知设置中的运营指令群绑定。"""
import hashlib
import json
from uuid import uuid4
from pathlib import Path

from ops_command_api import checked_id
from ops_commands import COMMANDS, GroupCommands
from ops_permission_import import FIELDS, LIMIT, inspect, read_rows, shop_labels, write_template

SECTIONS = {
    'help': ('帮助', ('help',)),
    'inventory': ('库存与价格', ('inventory.get', 'price.changes')),
    'monitor': ('监控设置', ('monitor.add', 'monitor.remove', 'monitor.threshold.set')),
    'orders': ('订单与发货', ('order.status.get', 'order.match', 'order.patrol', 'task.status', 'shipping.summary', 'front_stock.summary')),
}


def save(service, values):
    identity = [values.get(k) for k in ('org_id', 'robot_id', 'group_id')]
    name = values.get('name', '')
    if not all(map(checked_id, identity)) or not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('请先搜索并选择钉钉群')
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


def commit_members(service, identity, members, expected, names=None):
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        state = state_for(service, db, identity)
        if stamp(state) != expected:
            raise ValueError('群或成员权限已经变化，请重新打开权限设置或重新预览导入')
        key = GroupCommands.member_key(*identity)
        service.put(db, key, members)
        if names is not None:
            previous = service.get(db, key + ':member-names', {})
            service.put(db, key + ':member-names', {user: names.get(user, previous.get(user, '')) for user in members})
        if state_for(service, db, identity)['members'] != members:
            raise RuntimeError('成员权限回读不一致')
    return len(members)


def member_form(b, identity, state, user, name, *, member=None):
    from inventory_business_views import A, F
    rights = state['members'].get(user, dict(commands=[], shops=[], fields=[]))
    shops = available_shops(b)
    # 已停用店铺的旧权限仍可查看或清除，不会给其他成员加到该店。
    shops.update({k: '原授权店铺（'+k+'）' for k in rights['shops'] if k not in shops})
    shops = shop_labels(shops)
    fields = [F('commands', '可用指令', 'multi', compact=True, bulk=True,
                options=[(k, title if k in state['commands'] else title+'（群未开启）') for k,(title,_) in COMMANDS.items()]),
              F('shops', '可访问的店铺', 'multi', compact=True, bulk=True, options=sorted(shops.items(), key=lambda x:x[1])),
              F('fields', '附加权限', 'multi', compact=True, bulk=True, options=FIELDS.items())]
    if user in state['members']:
        fields.append(F('remove', '移除这名成员的全部权限', 'bool'))
    token = b.review('ops-member-edit', (identity,user,name,stamp(state),member))
    return A('ops-members-save', '编辑成员权限' if user in state['members'] else '设置成员权限', fields,
             dict(rights, token=token), module='settings', wide=True,
             description=f"{state['name']} · {name}。勾选后保存；群未开启的指令暂不可用。",
             submitLabel='保存权限')


def search_members(b, values, *, client=None):
    from inventory_business_views import A, F
    from ops_member_discovery import context, search
    identity = identity_for(values)
    state = current(b.service, identity)
    query = values.get('query')
    if not isinstance(query,str) or not 0 < len(query.strip()) <= 80:
        raise ValueError('请输入要添加的成员姓名或群昵称')
    directory, profile = context(b, identity, client)
    hits = search(directory.roster(profile, identity[2]), query)
    if not hits or len(hits)>50:
        raise ValueError('没有找到成员，请核对姓名或群昵称' if not hits else '结果太多，请输入更完整的姓名')
    token = b.review('ops-member-search', (identity,hits))
    return dict(nextForm=A('ops-members-choose', '选择群成员',
                [F('choice','搜索结果','pick',options=[(i,row['label']) for i,row in enumerate(hits)])],
                dict(token=token,choice=''), module='settings', submitLabel='设置权限',
                description=f"{state['name']} · 找到 {len(hits)} 位成员，点选要授权的人。"))


def choose_member(b, values, *, client=None):
    from ops_group_discovery import selected
    from ops_member_discovery import context
    identity, hits = b.reviewed(values.get('token'), 'ops-member-search')
    member = selected(hits, values)
    directory, profile = context(b, identity, client)
    user = directory.resolve(profile, member)
    state = current(b.service, identity)
    return dict(nextForm=member_form(b, identity, state, user, member['label'], member=member))


def list_members(b, values, *, client=None):
    from collections import Counter
    from inventory_business_views import A, F
    from ops_member_discovery import context
    identity = identity_for(values)
    state = current(b.service, identity)
    if not state['members']:
        raise ValueError('这个群还没有授权成员，请先点击「添加成员」')
    with b.service.db() as db:
        names = b.service.get(db,GroupCommands.member_key(*identity)+':member-names',{})
    missing = [user for user in state['members'] if not names.get(user)]
    if missing:
        # 历史权限可能只存了编号；查询失败时仍允许编辑或撤销原授权。
        try:
            directory, profile = context(b, identity, client)
            names.update(directory.existing_names(profile, missing))
        except (ValueError, OSError):
            pass
    display = {user: names.get(user) or '已有成员 · '+user[-8:] for user in state['members']}
    counts = Counter(display.values())
    rows = [(user, name if counts[name]==1 else name+' · '+user[-8:]) for user,name in display.items()]
    if len(rows)==1:
        return dict(nextForm=member_form(b,identity,state,*rows[0]))
    token = b.review('ops-member-existing', (identity,rows,stamp(state)))
    return dict(nextForm=A('ops-members-open', '编辑成员权限',
                [F('choice','已授权成员','pick',options=[(i,name) for i,(_,name) in enumerate(rows)])],
                dict(token=token,choice=''), module='settings', submitLabel='打开权限',
                description=f"{state['name']} · 选择一位成员，查看或修改已勾选的权限。"))


def open_member(b, values):
    from ops_group_discovery import selected
    identity, rows, expected = b.reviewed(values.get('token'), 'ops-member-existing')
    user, name = selected(rows, values)
    state = current(b.service, identity)
    if expected != stamp(state):
        raise ValueError('成员权限已变化，请重新打开')
    return dict(nextForm=member_form(b, identity, state, user, name))


def save_members(b, values, *, client=None):
    from ops_member_discovery import context
    identity,user,name,expected,member = b.reviewed(values.get('token'), 'ops-member-edit')
    state = current(b.service, identity)
    if stamp(state)!=expected:
        raise ValueError('群或成员权限已经变化，请重新打开权限设置')
    members = state['members']
    existing = members.get(user)
    if values.get('remove') is True:
        if existing is None:
            raise ValueError('该成员尚未授权')
        return commit_members(b.service, identity, {k:v for k,v in members.items() if k!=user}, expected, {})
    if existing is None:
        if not member or len(members)>=LIMIT:
            raise ValueError('请搜索真实群成员，单群最多可授权 200 人')
        directory,profile = context(b,identity,client)
        if member['open_id'] not in {r['open_id'] for r in directory.roster(profile,identity[2]).values()}:
            raise ValueError('这名成员已不在当前群，未授予权限')
    shop_scope = set(available_shops(b)) | set((existing or {}).get('shops',[]))
    rights = {}
    for key, choices in (('commands', COMMANDS),('shops',shop_scope),('fields',FIELDS)):
        chosen = values.get(key)
        if not isinstance(chosen,list) or any(not isinstance(k,str) or k not in choices for k in chosen):
            raise ValueError('选项已变化，请重新打开权限设置')
        rights[key] = list(dict.fromkeys(chosen))
    return commit_members(b.service, identity, {**members,user:rights}, expected, {user:name})


def template(b, values, *, client=None):
    from ops_member_discovery import context
    identity = identity_for(values)
    current(b.service, identity)
    directory, profile = context(b,identity,client)
    roster = directory.roster(profile,identity[2])
    if not roster:
        raise ValueError('群里没有可选成员，暂不能生成模板')
    path = Path(b.service.root) / 'console_exports' / ('ops-' + stamp(identity)[:20]) / uuid4().hex / '运营机器人权限导入模板.xlsx'
    path.parent.mkdir(parents=True, exist_ok=True)
    write_template(path, stamp(identity), available_shops(b), roster)
    return dict(message='在表格中选成员和权限，保存后回到同一个群导入。', download=b.review('download', path), filename=path.name)


def preview_import(b, values, *, client=None):
    from inventory_business_views import T, A
    from ops_member_discovery import context
    identity = identity_for(values)
    state = current(b.service, identity)
    shops = available_shops(b)
    parsed, errors = read_rows(values.get('path'),stamp(identity),shops)
    if parsed:
        directory, profile = context(b,identity,client)
        roster = directory.roster(profile,identity[2])
        members, rows, names, errors = inspect(parsed,errors,state['members'],roster,lambda row:directory.resolve(profile,row),shops)
    else:
        members, rows, names = state['members'], [], {}
    tables = []
    if rows:
        tables.append(T('成员权限变更预览', [('line','表格行'), ('member','群成员'), ('operation','处理'),
                                             ('commands','可用指令'), ('shops','店铺'), ('fields','附加权限')], rows))
    if errors:
        tables.append(T('待修正的表格行', [('line','表格行'), ('reason','原因')], errors))
        return dict(message=f'发现 {len(errors)} 处问题；修正后重新导入。本次未保存。', tables=tables)
    added = sum(row['operation']=='新增' for row in rows)
    changed = sum(row['operation']=='变更' for row in rows)
    name = state['name'] or '当前群'
    forms = []
    if added or changed:
        token = b.review('ops-members-import', (identity,members,names,stamp(state),shops,
                                               {open_id for _,_,open_id,_ in parsed}))
        forms.append(A('ops-members-import-confirm', '确认导入权限', values=dict(token=token),
                       confirm=f'群：{name}。新增 {added} 人，更新 {changed} 人；表格未列出的成员保持原权限。'))
    return dict(message=f'{name}：新增 {added} 人，变更 {changed} 人，无变化 {len(rows)-added-changed} 人。尚未保存。',
                tables=tables, forms=forms)


def confirm_import(b, values, *, client=None):
    from ops_member_discovery import context
    identity,members,names,expected,shops,open_ids = b.reviewed(values.get('token'), 'ops-members-import')
    if shops != available_shops(b):
        raise ValueError('当前工作区的店铺范围已变化，请重新预览导入')
    if stamp(current(b.service,identity)) != expected:
        raise ValueError('群或成员权限已经变化，请重新预览导入')
    directory, profile = context(b,identity,client)
    if not open_ids <= {r['open_id'] for r in directory.roster(profile,identity[2]).values()}:
        raise ValueError('部分成员已退出当前群，请重新预览导入')
    return commit_members(b.service,identity,members,expected,names)


def binding_form(b, identity, name, *, robot_name='', token=None, commands=None):
    from inventory_business_views import F, A

    if commands is None:
        # 已绑定群重新识别时沿用勾选；尚未绑定才使用当前可用功能的默认值。
        commands = GroupCommands(b.service).get(*identity_for(identity))
    if commands is None:
        api = getattr(b, 'ops_command_api', None)
        commands = (getattr(api.business, 'available', api.business.implemented) if api else
                    {'help', 'inventory.get', 'price.changes', 'monitor.add', 'monitor.remove', 'monitor.threshold.set'})
    values = dict(identity, name=name)
    if token:
        values['token'] = token
    values.update({key: [k for k in choices if k in commands] for key, (_, choices) in SECTIONS.items()})
    fields = [F(key,label,'multi',options=[(k,COMMANDS[k][0]) for k in choices], bulk=True, compact=True)
              for key,(label,choices) in SECTIONS.items()]
    description = f'{name} · {robot_name}' if robot_name else name
    return A('ops-group-save', '确认群绑定' if token else '编辑群功能', fields, values, module='settings',
             description=description+'。勾选允许在此群使用的功能。', submitLabel='保存群设置', wide=True)


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
            identity = dict(org_id=org, robot_id=robot, group_id=group)
            name = b.service.get(db,key+':name','')
            rows.append(dict(id=json.dumps([org,robot,group]), name=name, robot_id=robot, group_id=group, member_count=len(grants),
                             features='、'.join(COMMANDS[k][0] for k in selected) or '未开启',
                             _forms=[binding_form(b,identity,name,commands=selected),
                                     A('ops-members-search','添加成员',[F('query','成员姓名或群昵称')],
                                       identity,submitLabel='搜索群成员',
                                       description=f'{name} · 搜索本群成员，选中后勾选权限。'),
                                     A('ops-members-list','编辑成员权限',values=identity,immediate=True),
                                     A('ops-members-template','下载权限模板',values=identity,immediate=True),
                                     A('ops-members-import-preview','导入权限表格',
                                       [F('path','权限表格（.xlsx）','file',pickOnly=True)],identity,submitLabel='预览权限变更',
                                       description=f'{name} · 选择填写好的模板，确认预览后生效。')]))
    result = S('ops-commands','群聊与成员权限')
    status = ('机器人指令已接入。' if api else '运营指令接入配置无效，请核对本机配置。' if
              getattr(b,'ops_command_config_error',False) else '机器人指令尚未接入。')
    result['description'] = status+' 先为群开启功能，再给成员勾选权限。'
    result['forms'] = [A('ops-group-discover','绑定钉钉群',[F('name','群名称')],submitLabel='识别群信息',
                         description='输入钉钉群名，选择要绑定的群。')]
    result['tables'] = [T('群聊与成员权限',[('name','群名称'),('robot_id','接收机器人标识'),('features','已开启功能'),('member_count','授权成员')],rows,
                          widths=dict(name=140,features=235,member_count=85))]
    from ops_tax_cost_settings import panel as cost_panel
    cost_forms,cost_tables = cost_panel(b)
    result['forms'].extend(cost_forms)
    result['tables'].extend(cost_tables)
    # 展示现用查询连接，管理员不用从群回复猜数据来自哪张表。
    if api:
        business = api.business
        detail = getattr(getattr(business,'reports',None),'detail',{})
        connections = [dict(source=label,scope='在线表格',usage=usage)
                       for key,label,usage in (
            ('shipping_orders','待揽收订单汇总','发货监控汇总'),
            ('shipping_shortages','缺货情况','发货监控汇总'),
            ('front_stock','补库存明细','前台库存监控')) if key in detail]
        connections.extend(dict(source=Path(row['path']).name,scope=row['sheet'],usage='查询商品 · '+row['host_price_column'])
                           for row in getattr(getattr(business,'inventory',None),'maintenance',()) if row.get('host_price_column'))
        if connections:
            result['tables'].append(T('已配置的查询来源',[('source','表格'),('scope','工作表'),
                                    ('usage','用于查询')],connections,compact=True))
    return result
