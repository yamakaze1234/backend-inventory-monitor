"""设置导航组合现有配置入口，保存仍使用原业务模块和身份核验。"""
from inventory_business_views import build_view, S


def routed(section, module):
    # 行内操作和后续表单必须保留所属模块，避免合并导航后提交到错误入口。
    for form in section['forms']:
        form.setdefault('module', module)
    for table in section['tables']:
        for row in table['rows']:
            for form in row.get('_forms', []):
                form.setdefault('module', module)
    return section


def view(b, module, query):
    if module == 'cache-settings':
        from inventory_cache_schedule import settings_section, schedule_form, settings
        cache = routed(settings_section(b), 'settings')
        cache['title'] = '缓存清理'
        cache['forms'].append(schedule_form(settings(b.service)))
        return dict(module=module, filters=[], query=query, sections=[cache])
    legacy = {s['id']: routed(s, 'settings') for s in build_view(b, 'settings', query)['sections']}
    from order_business import view as order_view
    order = {s['id']: routed(s, 'order-settings') for s in order_view(b, query)['sections']}
    robots = S('robot-binding', '机器人、群聊与权限',
               description='管理通知机器人和接单机器人；绑定群聊后，设置群功能及每位成员的指令、店铺和数据权限。')
    robots['text'] = order['robot']['text'] + '\n' + legacy['ops-commands']['description']
    # 同页展示机器人、群和权限操作，不再藏在其他配置分区。
    robots['forms'] = [*legacy['robots']['forms'], *order['robot']['forms'], *legacy['ops-commands']['forms']]
    robots['tables'] = [*legacy['ops-commands']['tables'], *legacy['robots']['tables']]
    sections = [robots, order['backend'], legacy['source'], order['daily-report'], legacy['workspace'], legacy['startup']]
    return dict(module=module, filters=[], query=query, sections=sections)
