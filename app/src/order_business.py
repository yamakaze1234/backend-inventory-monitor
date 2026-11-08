"""订单匹配专属配置页：后端、队列及接单机器人独立于库存通知。"""
from inventory_business_views import A,F,S,T


def adapter(b):
    if not getattr(b,'order_bot',None):
        from order_bot_adapter import OrderBotAdapter
        b.order_bot=OrderBotAdapter(workspace_root=b.service.root)
    return b.order_bot


def view(b,q):
    state=adapter(b).configuration()
    backend=S('backend','匹配服务配置')
    backend['description']='为本部门连接独立匹配后端和运行队列；保存配置不会启动服务或修改 ERP。'
    labels=[('match_root','匹配项目目录'),('control_root','机器人控制目录'),('state_root','本部门运行数据目录'),('python_path','Python 程序路径'),('dws_path','DWS 程序路径'),('pwsh_path','PowerShell 7 程序路径'),('node_path','Node 程序路径'),('agent_model','AI 模型标识')]
    values={k:state.get(k,'') for k,_ in labels};values['shops']=state.get('shops') or [dict(id='',name='')]
    backend['forms']=[A('backend-save','保存匹配服务配置',[*[F(k,label) for k,label in labels],F('shops','本部门匹配店铺','rows',columns=[('id','ERP 店铺编号'),('name','店铺名称')])],values),
        A('backend-deploy','安装部门兼容后端',confirm='将备份并更新所选后端的兼容文件。必须先停止原订单服务并核对未完成任务；本操作不会启动匹配或发送消息。')]
    backend['text']=state.get('message','')
    robot=S('robot','订单匹配机器人')
    robot['description']='负责群内接单与订单匹配，需要企业应用的 Stream 能力；与库存、调价通知机器人分别配置。'
    target=state.get('target') or {}
    robot['text']='当前接单机器人：'+str(target.get('robotName') or '未配置')+'；群：'+str(target.get('groupName') or '未配置')
    if state.get('deployed'):
        robot['forms']=[A('robot-search','查找并配置匹配机器人',[F('groupQuery','群名称'),F('robotQuery','机器人名称'),F('intervalMinutes','订单巡检间隔（分钟）','number')],dict(groupQuery=target.get('groupName',''),robotQuery=target.get('robotName',''),intervalMinutes=target.get('intervalMinutes',120)))]
    else:robot['text']+='。请先保存匹配服务配置并安装兼容后端。'
    return dict(module='order-settings',filters=[],query=q,sections=[backend,robot])


def save_backend(b,v):
    adapter(b).save_configuration(v)
    return dict(message='本部门订单运行配置已保存；服务未启动')


def deploy_backend(b,v):
    b.external();adapter(b).deploy_backend()
    return dict(message='部门兼容后端已安装，原文件已备份；服务未启动')


def _save_target(b,plan,robot,group):
    result=adapter(b).action('save-robot',dict(appId=robot['appId'],groupId=group['groupId'],groupName=group['name'],profile=plan['profile'],intervalMinutes=plan['intervalMinutes']))
    return dict(message=str(result.get('message') or '匹配机器人配置已提交，请以接单状态为准'))


def search_robot(b,v):
    b.external()
    group_name=str(v.get('groupQuery','')).strip();robot_name=str(v.get('robotQuery','')).strip()
    if not group_name or not robot_name:raise ValueError('请填写群名称和机器人名称')
    try:minutes=int(v.get('intervalMinutes',120))
    except (TypeError,ValueError):raise ValueError('巡检间隔需为整数分钟') from None
    if not 15<=minutes<=1440:raise ValueError('巡检间隔需为 15 至 1440 分钟')
    result=adapter(b).action('discover-robots',dict(groupQuery=group_name,robotQuery=robot_name))
    if result.get('moreRobots'):raise ValueError('匹配机器人结果未完整返回，请填写更完整名称')
    robots=result.get('robots') or [];groups=result.get('groups') or []
    if not robots or not groups:raise ValueError('未找到可用的群或 Stream 机器人，请核对名称与账号权限')
    exact_robots=[r for r in robots if r['name']==robot_name];exact_groups=[r for r in groups if r['name']==group_name]
    robots=exact_robots or robots;groups=exact_groups or groups
    plan=dict(profile=result['profile'],intervalMinutes=minutes,robots=robots,groups=groups)
    if len(exact_robots)==1 and len(exact_groups)==1:return _save_target(b,plan,robots[0],groups[0])
    token=b.review('order-robot',plan)
    return dict(message='存在重名或相近结果，请选择实际群和匹配机器人',forms=[A('robot-select','确认匹配机器人',[F('robot','机器人',options=[(str(i),r['name']+' · '+r['appId'][-8:]) for i,r in enumerate(robots)]),F('group','群',options=[(str(i),r['name']+' · '+r['groupId'][-8:]) for i,r in enumerate(groups)])],dict(token=token))])


def select_robot(b,v):
    b.external();plan=b.reviewed(v.get('token'),'order-robot')
    try:robot=int(v['robot']);group=int(v['group'])
    except (ValueError,TypeError,KeyError):raise ValueError('请选择群和机器人') from None
    if not 0<=robot<len(plan['robots']) or not 0<=group<len(plan['groups']):raise ValueError('选择已无效，请重新查询')
    return _save_target(b,plan,plan['robots'][robot],plan['groups'][group])
