"""Read-only target discovery and validated settings; credentials stay in DWS."""
import json
from robot_config import DEFAULT, get_config, validate


def current_profile(run, executable):
    # 每次发现按 DWS 当前组织唯一解析，不选第一项或默认个人账号。
    value=json.loads(run([executable,'profile','list','--format','json'],timeout=15))
    rows=[p['profile'] for p in value.get('profiles',[]) if p.get('isOrgCurrent') is True and p.get('profile')]
    if len(rows)!=1:raise ValueError('请先在钉钉选择唯一当前组织账号')
    return rows[0]


def dws_read(run, executable, args, profile):
    value=json.loads(run([executable,*args,'--profile',profile,'--timeout','10','--format','json'],timeout=15))
    if value.get('error') or value.get('ok') is False or value.get('success') is False:
        raise ValueError('钉钉查询失败，请检查当前组织登录状态')
    return value


def discover(payload, run, executable):
    robot=str(payload.get('robotQuery','')).strip()
    group=str(payload.get('groupQuery','')).strip()
    if not robot or not group: raise ValueError('请填写机器人和群的搜索关键词')
    profile=current_profile(run,executable)
    apps=dws_read(run,executable,['dev','app','list','--robot-name',robot,'--page-size','50'],profile)
    groups=dws_read(run,executable,['chat','+chat-search','--query',group,'--page-all'],profile)
    if not groups.get('complete') or groups.get('hasMore'):
        raise ValueError('群搜索未完整返回，请缩小关键词后重试')
    return {'profile':profile,'robots':[{'appId':x['unifiedAppId'],'name':x.get('robotName') or x['name']} for x in apps.get('data',{}).get('items',[])],
            'groups':[{'groupId':x['openConversationId'],'name':x.get('title') or x['name']} for x in groups.get('chats',[])],
            'moreRobots': not apps.get('meta',{}).get('pagination',{}).get('endpoint_exhausted',False)}


def validated_target(payload, run, executable):
    app_id=str(payload.get('appId',''));group_id=str(payload.get('groupId',''))
    # Syntax check before passing arguments to the CLI. Names are read back below.
    profile=current_profile(run,executable)
    if payload.get('profile') and payload['profile']!=profile:raise ValueError('当前组织已变化，请重新查找机器人')
    config=dict(DEFAULT,profile=profile,appId=app_id,groupId=group_id,robotCode='pending',robotName='待核验',groupName='待核验',intervalMinutes=payload.get('intervalMinutes'))
    validate(config)
    app=dws_read(run,executable,['dev','app','get','--unified-app-id',app_id],profile).get('data',{})
    robot=dws_read(run,executable,['dev','app','robot','get','--unified-app-id',app_id],profile).get('data',{})
    if app.get('unifiedAppId')!=app_id or robot.get('unifiedAppId')!=app_id or app.get('appStatus')!='published':
        raise ValueError('请选择当前组织已发布的应用机器人')
    if not robot.get('configured') or robot.get('mode')!='STREAM' or robot.get('robotStatus')!='ONLINE':
        raise ValueError('机器人须已配置 Stream 接收模式并处于在线发布状态')
    if robot.get('robotCode')!=app.get('appKey'): raise ValueError('机器人与应用标识不一致')
    # Re-resolve the selected group by its name and compare its stable ID.
    groups=dws_read(run,executable,['chat','+chat-search','--query',str(payload.get('groupName','')),'--page-all'],profile)
    if not groups.get('complete'): raise ValueError('群信息未完整返回')
    selected=[x for x in groups.get('chats',[]) if x.get('openConversationId')==group_id]
    if len(selected)!=1: raise ValueError('目标群已变化，请重新查找并选择')
    members=dws_read(run,executable,['chat','+chat-members-list','--group',group_id],profile)
    if members.get('complete') is not True or members.get('hasMore') or members.get('partial') or members.get('failures') or members.get('conversationId')!=group_id:
        raise ValueError('群成员信息未完整返回，请重新查询')
    if len([x for x in members.get('bots',[]) if x.get('robotCode')==robot.get('robotCode') and x.get('name')==robot.get('name')])!=1:
        raise ValueError('目标群内未唯一找到该机器人，请先在钉钉中添加机器人')
    config.update(robotName=robot['name'],robotCode=robot['robotCode'],groupName=selected[0].get('title') or selected[0]['name'])
    return validate(config)
