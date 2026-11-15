"""订单匹配专属配置页：后端、队列及接单机器人独立于库存通知。"""
import json
from pathlib import Path
import shutil
import sys
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
    backend['forms']=[A('backend-auto','一键配置',immediate=True), backend_form(state,'手动配置'),
        A('backend-deploy','安装部门兼容后端',confirm='将备份并更新所选后端的兼容文件。必须先停止原订单服务并核对未完成任务；本操作不会启动匹配或发送消息。')]
    backend['text']=state.get('message','')
    robot=S('robot','订单匹配机器人')
    robot['description']='负责群内接单与订单匹配，需要企业应用的 Stream 能力；与库存、调价通知机器人分别配置。'
    target=state.get('target') or {}
    robot['text']='当前接单机器人：'+str(target.get('robotName') or '未配置')+'；群：'+str(target.get('groupName') or '未配置')
    if state.get('deployed'):
        robot['forms']=[A('robot-search','查找并配置匹配机器人',[F('groupQuery','群名称'),F('robotQuery','机器人名称')],dict(groupQuery=target.get('groupName',''),robotQuery=target.get('robotName','')))]
        if state.get('patrolWindowReady'):
            robot['planLink']='orders'
        else:
            robot['text']+='；自动巡检时段需要新版兼容后端。'
    else:robot['text']+='。请先保存匹配服务配置并安装兼容后端。'
    from daily_report_business import view as daily_view
    return dict(module='order-settings',filters=[],query=q,sections=[backend,robot,daily_view(b.service.root)])


def patrol_window_form(state):
    from order_bot_config import patrol_window_view
    if not state.get('deployed') or not state.get('patrolWindowReady'):
        return None
    target=state.get('target') or {}
    window=state.get('patrolWindow') or patrol_window_view(None,target.get('intervalMinutes'))
    return A('patrol-window','设置自动巡检时段',
             [F('start','开始时间','time'),F('end','结束时间','time'),F('intervalMinutes','巡检间隔（分钟）','number')],
             dict(start=window['start'],end=window['end'],intervalMinutes=window['intervalMinutes']),
             module='order-settings',
             description='北京时间。结束早于开始时按次日处理；到点后不再开始新一轮，已开始的巡检会正常完成。',
             submitLabel='保存设置')


def save_patrol_window(b,v):
    from order_bot_config import patrol_window_tools
    tools=patrol_window_tools()
    start,end=tools['validate'](v.get('start'),v.get('end'))
    interval=tools['validate_interval'](v.get('intervalMinutes'))
    result=adapter(b).action('save-patrol-window',dict(start=start,end=end,intervalMinutes=interval))
    return dict(message=f"自动巡检设置已保存：{result['start']} 至 {('次日 ' if result['nextDay'] else '')+result['end']}（北京时间），每 {result['intervalMinutes']} 分钟")


def shipping_action(b, operation):
    # 单独控制发货巡检，不改变订单匹配计划或自动启动订单服务。
    b.external()
    bot=adapter(b)
    state=bot.configuration()
    if not state.get('deployed') or not state.get('shippingReady'):
        raise ValueError('请先安装支持发货监控的订单兼容后端')
    if operation!='shipping-off':
        # 桌面已更新但外置执行器未更新时，不再提交会发送旧格式的巡检。
        if state.get('shippingBackendCurrent') is False:
            raise ValueError('请停止订单服务，并到订单机器人设置安装部门兼容后端')
        if not state.get('shipping',{}).get('bound') or state.get('switching'):
            raise ValueError('请先确认发货提醒目标群，并等待机器人切换完成')
    if operation=='shipping' and (not state.get('online') or not state.get('serviceEnabled') or state.get('stopRequested')):
        raise ValueError('订单执行器未运行，发货巡检未提交')
    result=bot.action(operation)
    if operation=='shipping':
        if not result.get('jobId'):
            raise ValueError('未收到发货巡检入队回执，请先查看任务记录')
        if result.get('duplicate'):
            return dict(message='已有发货巡检记录，本次未重复提交，请到订单匹配任务记录查看。')
        return dict(message='发货巡检已入队，请到订单匹配任务记录查看结果。')
    return result


def backend_form(state,title):
    labels=[('match_root','匹配项目目录'),('control_root','机器人控制目录'),('state_root','本部门运行数据目录'),('python_path','Python 程序路径'),('dws_path','DWS 程序路径'),('pwsh_path','PowerShell 7 程序路径'),('node_path','Node 程序路径'),('agent_model','AI 模型标识')]
    values={k:state.get(k,'') for k,_ in labels};values['shops']=state.get('shops') or [dict(id='',name='')]
    return A('backend-save',title,[*[F(k,label) for k,label in labels],F('shops','本部门匹配店铺','rows',columns=[('id','ERP 店铺编号'),('name','店铺名称')])],values)


def auto_backend(b,v):
    from order_bot_config import PATH_FIELDS
    bot=adapter(b)
    state=bot.configuration()
    if state.get('configured'):return dict(message='匹配服务已配置，可通过手动配置修改。')
    # 只检查安装位置附近的约定目录，不全盘搜索或接管旧队列。
    install=Path(sys.executable if getattr(sys,'frozen',False) else __file__).resolve()
    parents=install.parents
    def unique(suffixes,required):
        paths={p.resolve() for parent in parents for suffix in suffixes if (p:=parent/suffix).is_dir() and (p/required).is_file()}
        return str(next(iter(paths))) if len(paths)==1 else ''
    match=unique(('匹配/抖店SKU查询/抖店SKU查询','matching',''),'app/src/group_order_bot.py')
    control=unique(('数据分析/机器人日报','PythonProject/数据分析/机器人日报','control',''),'robot_desktop_control.py')
    def executable(name,*fallback):
        paths=[shutil.which(name),*fallback]
        return next((str(Path(p).resolve()) for p in paths if p and 'windowsapps' not in str(p).lower() and Path(p).is_file()),'')
    values=dict(match_root=match,control_root=control,state_root=str((b.service.root/'order_matching').resolve()),
                python_path=executable('python.exe',Path(install.anchor)/'python/python.exe'),dws_path=executable('dws.exe',Path.home()/'.local/bin/dws.exe'),
                pwsh_path=executable('pwsh.exe'),node_path=executable('node.exe'))
    shops=[]
    if match:
        try:
            data=json.loads((Path(match)/'app/data/维护表设置.json').read_text(encoding='utf-8-sig'))
            if data.get('version')==1:
                shops=[dict(id=k,name=r['name']) for k,r in data['shops'].items() if r.get('name') and r.get('sheet') and Path(r.get('path','')).is_absolute()]
        except (OSError,ValueError,TypeError,AttributeError,KeyError):pass
    values['shops']=shops
    # 自动发现的店铺范围需要回显，避免在共享匹配项目中默认接管其他部门店铺。
    complete=all(values.get(key) for key in PATH_FIELDS) and bool(shops)
    message='已识别运行环境和店铺，请核对本部门范围后保存。' if complete else '已填入可识别的配置，请补齐空项并核对本部门店铺。'
    return dict(message=message,forms=[backend_form(values,'核对并保存配置')])


def save_backend(b,v):
    adapter(b).save_configuration(v)
    return dict(message='本部门订单运行配置已保存；服务未启动')


def deploy_backend(b,v):
    b.external();adapter(b).deploy_backend()
    return dict(message='部门兼容后端已安装，原文件已备份；服务未启动')


def _save_target(b,plan,robot,group):
    bot=adapter(b)
    # 绑定机器人保留旧路由间隔；它也可能是发货巡检的默认间隔。
    target=bot.configuration().get('target') or {}
    from order_bot_config import patrol_window_tools
    legacy=target.get('intervalMinutes') or patrol_window_tools()['DEFAULT_INTERVAL']
    result=bot.action('save-robot',dict(appId=robot['appId'],groupId=group['groupId'],groupName=group['name'],
                                       profile=plan['profile'],intervalMinutes=legacy))
    return dict(message=str(result.get('message') or '匹配机器人配置已提交，请以接单状态为准'))


def search_robot(b,v):
    b.external()
    group_name=str(v.get('groupQuery','')).strip();robot_name=str(v.get('robotQuery','')).strip()
    if not group_name or not robot_name:raise ValueError('请填写群名称和机器人名称')
    result=adapter(b).action('discover-robots',dict(groupQuery=group_name,robotQuery=robot_name))
    if result.get('moreRobots'):raise ValueError('匹配机器人结果未完整返回，请填写更完整名称')
    robots=result.get('robots') or [];groups=result.get('groups') or []
    if not robots or not groups:raise ValueError('未找到可用的群或 Stream 机器人，请核对名称与账号权限')
    exact_robots=[r for r in robots if r['name']==robot_name];exact_groups=[r for r in groups if r['name']==group_name]
    robots=exact_robots or robots;groups=exact_groups or groups
    plan=dict(profile=result['profile'],robots=robots,groups=groups)
    if len(exact_robots)==1 and len(exact_groups)==1:return _save_target(b,plan,robots[0],groups[0])
    token=b.review('order-robot',plan)
    return dict(message='存在重名或相近结果，请选择实际群和匹配机器人',forms=[A('robot-select','确认匹配机器人',[F('robot','机器人',options=[(str(i),r['name']+' · '+r['appId'][-8:]) for i,r in enumerate(robots)]),F('group','群',options=[(str(i),r['name']+' · '+r['groupId'][-8:]) for i,r in enumerate(groups)])],dict(token=token))])


def select_robot(b,v):
    b.external();plan=b.reviewed(v.get('token'),'order-robot')
    try:robot=int(v['robot']);group=int(v['group'])
    except (ValueError,TypeError,KeyError):raise ValueError('请选择群和机器人') from None
    if not 0<=robot<len(plan['robots']) or not 0<=group<len(plan['groups']):raise ValueError('选择已无效，请重新查询')
    return _save_target(b,plan,plan['robots'][robot],plan['groups'][group])
