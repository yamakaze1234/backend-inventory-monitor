"""运营机器人设置：通过钉钉群名解析组织、群和已入群的机器人。"""
from pathlib import Path

from inventory_business_views import A, F
from notification_enterprise import EnterpriseClient, validate_target
from ops_command_api import checked_id


class DiscoveryClient(EnterpriseClient):
    def groups(self, profile, query):
        # 重名必须能展示完整候选，不能因为首屏只有一条而自动选中。
        value = self.call(['chat', '+chat-search', '--query', query, '--page-all'], profile)
        self.complete(value)
        return [dict(group_id=g['openConversationId'], group_name=g.get('name') or g.get('title'))
                for g in value.get('chats', []) if g.get('openConversationId') and (g.get('name') or g.get('title'))]


def profile_org(profile):
    # DWS profile 的稳定标识是 corpId:userId，显示名称不用于授权或保存。
    org, sep, user = profile.partition(':')
    if not sep or not checked_id(org) or not checked_id(user) or ':' in user:
        raise ValueError('钉钉账号缺少可核实的组织标识')
    return org


def preferred(b):
    from order_bot_config import load, queue_state

    api = getattr(b, 'ops_command_api', None)
    robot = getattr(api, 'robot_id', None)
    org = getattr(api, 'org_id', None)
    config = load(b.service.root)
    if not config:
        return org, robot, None
    target = queue_state(Path(config['state_root']))['target']
    profile, configured_robot = target.get('profile'), target.get('robotCode')
    if profile and configured_robot and (not robot or robot == configured_robot) and (not org or org == profile_org(profile)):
        return profile_org(profile), configured_robot, profile
    return org, robot, None


def choose(b, kind, title, label, options, values, message):
    token = b.review('ops-group-' + kind, values)
    return dict(message=message, nextForm=A('ops-group-' + kind, title,
                [F('choice', label, options=options)], dict(token=token, choice=''), module='settings', submitLabel='确认选择'))


def ready(b, target):
    from ops_settings import binding_form

    target = validate_target(target)
    identity = dict(org_id=profile_org(target['profile']), robot_id=target['robot_code'], group_id=target['group_id'])
    token = b.review('ops-group-verified', dict(identity, name=target['group_name']))
    return dict(message=f"已识别：{target['group_name']} / {target['robot_name']}。请核对后保存。",
                nextForm=binding_form(b, identity, target['group_name'], robot_name=target['robot_name'], token=token))


def find_bots(b, client, profile, group, robot):
    bots = list({r['robot_code']:r for r in client.bots(profile, group['group_id'])
                 if checked_id(r.get('robot_code')) and r['robot_code'].startswith('ding')
                 and isinstance(r.get('robot_name'), str) and r['robot_name'].strip()}.values())
    if robot:
        bots = [r for r in bots if r['robot_code'] == robot]
    if not bots:
        raise ValueError('未在此群找到当前运营机器人，请核对群名称和群内机器人')
    targets = [dict(profile=profile, **group, **r) for r in bots]
    if len(targets) == 1:
        return ready(b, targets[0])
    return choose(b, 'robot-choice', '选择群内机器人', '机器人',
                  [(str(i), r['robot_name'] + ' · ' + r['robot_code'][-10:]) for i,r in enumerate(targets)], targets,
                  '群内有多个企业机器人，请选择用于接收指令的机器人。')


def find_groups(b, client, name, profile, robot):
    groups = list({r['group_id']:r for r in client.groups(profile, name)
                   if checked_id(r.get('group_id')) and r['group_id'].startswith('cid')
                   and isinstance(r.get('group_name'), str) and r['group_name'].strip()}.values())
    exact = [g for g in groups if g['group_name'] == name]
    if not groups:
        raise ValueError('没有找到此群，请核对群名称及当前钉钉账号是否在群中')
    if len(exact) == 1:
        return find_bots(b, client, profile, exact[0], robot)
    candidates = exact or groups
    if len(candidates) > 30:
        raise ValueError('候选群过多，请输入更完整的群名称')
    return choose(b, 'group-choice', '选择实际钉钉群', '钉钉群',
                  [(str(i), g['group_name'] + ' · ' + g['group_id'][-10:]) for i,g in enumerate(candidates)],
                  dict(groups=candidates, profile=profile, robot=robot),
                  '存在同名或相近群，请选择准确的群。')


def discover(b, values, *, client=None):
    b.external()
    name = values.get('name')
    if not isinstance(name,str) or not name.strip() or len(name.strip())>100:
        raise ValueError('请输入要绑定的钉钉群名称')
    name = name.strip()
    client = client or DiscoveryClient()
    org, robot, pinned = preferred(b)
    profiles = [r for r in client.profiles() if not org or profile_org(r['profile']) == org]
    if not profiles:
        raise ValueError('当前机器人所在组织没有可用的钉钉账号')
    if pinned:
        if pinned not in {r['profile'] for r in profiles}:
            raise ValueError('当前运营机器人使用的钉钉账号未登录')
        return find_groups(b, client, name, pinned, robot)
    defaults = [r for r in profiles if r.get('current')]
    if len(defaults) == 1:
        return find_groups(b, client, name, defaults[0]['profile'], robot)
    return choose(b, 'account-choice', '选择钉钉账号', '组织与账号',
                  [(str(i),r['label']) for i,r in enumerate(profiles)], dict(profiles=profiles, name=name, robot=robot),
                  '当前没有唯一的组织账号，请选择本次查找使用的账号。')


def selected(items, values):
    value = values.get('choice')
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value)>4:
        raise ValueError('请选择列表中的候选项')
    index = int(value)
    if index >= len(items):
        raise ValueError('选择已失效，请重新识别')
    return items[index]


def resolve_choice(b, values, kind, *, client=None):
    b.external()
    client = client or DiscoveryClient()
    pending = b.reviewed(values.get('token'), 'ops-group-' + kind)
    if kind == 'account-choice':
        profile = selected(pending['profiles'], values)['profile']
        if profile not in {r['profile'] for r in client.profiles()}:
            raise ValueError('所选钉钉账号已不可用，请重新识别')
        return find_groups(b, client, pending['name'], profile, pending['robot'])
    if kind == 'group-choice':
        group = selected(pending['groups'], values)
        profile = pending['profile']
        if group not in client.groups(profile, group['group_name']):
            raise ValueError('目标群已变化，请重新识别')
        return find_bots(b, client, profile, group, pending['robot'])
    if kind == 'robot-choice':
        target = selected(pending, values)
        return ready(b, client.verify(target))
    raise ValueError('未知候选类型')
