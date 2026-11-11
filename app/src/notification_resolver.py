"""企业通知机器人按名称解析；账号与候选均来自 DWS，绝不猜测目标。"""


def resolve(client, group_name, robot_name, profile=None):
    group_name=str(group_name or '').strip();robot_name=str(robot_name or '').strip()
    if not group_name or not robot_name:
        raise ValueError('请填写群名称和机器人名称')
    if len(group_name)>100 or len(robot_name)>100:
        raise ValueError('群名称和机器人名称不能超过 100 字')
    profiles=client.profiles()
    if not profiles:raise ValueError('尚未登录钉钉账号，请先完成钉钉登录')
    if profile:
        if not any(r['profile']==profile for r in profiles):raise ValueError('所选钉钉账号已不可用，请重新选择')
    else:
        current=[r for r in profiles if r['current']]
        if len(current)!=1:return dict(profiles=profiles,targets=[],exact=False)
        profile=current[0]['profile']
    groups=client.groups(profile,group_name)
    exact_groups=[g for g in groups if g['group_name']==group_name]
    groups=exact_groups or groups
    if not groups:raise ValueError('未找到该群，请核对群名称及当前账号是否已加入')
    if len(groups)>20:raise ValueError('同名或相近群过多，请填写更完整的群名称')
    targets=[]
    for group in groups:
        bots=client.bots(profile,group['group_id'])
        exact_bots=[r for r in bots if r['robot_name']==robot_name]
        for bot in exact_bots or [r for r in bots if robot_name.casefold() in r['robot_name'].casefold()]:
            targets.append(dict(profile=profile,**group,**bot))
    # 同名实体保留不同标识供人工选择；相同实体重复返回则去重。
    targets=list({(r['profile'],r['group_id'],r['robot_code']):r for r in targets}.values())
    if not targets:raise ValueError('群内未找到该企业机器人，请核对名称及是否已添加到群')
    return dict(profiles=[],targets=targets,exact=len({g['group_id'] for g in groups})==1 and len(targets)==1 and targets[0]['group_name']==group_name and targets[0]['robot_name']==robot_name)


def verify_selection(client,target):
    """选择候选后再次核对账号、群和机器人，避免过期选择或名称变更。"""
    result=resolve(client,target['group_name'],target['robot_name'],target['profile'])
    if target not in result['targets']:raise ValueError('群或机器人已变化，请重新搜索')
    return target
