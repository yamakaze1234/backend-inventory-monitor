"""名称搜索到通知配置的工作流；候选保存在服务端，不接收手填内部标识。"""
from inventory_business_views import A,F,T
from notification_enterprise import EnterpriseClient
from notification_resolver import resolve,verify_selection


def _values(values):
    # 技术标识只能来自解析结果，表单隐藏字段也不能绕过身份校验。
    return {k:values[k] for k in ('id','name','group_name','robot_name','channels','enabled') if k in values}


def _save(b,values,target):
    channels=values.get('channels') or []
    if not isinstance(channels,list):raise ValueError('请选择通知类型')
    name=str(values.get('name') or '').strip() or (target['robot_name']+' · '+target['group_name'])[:60]
    b.robots().save(name,channels=channels,robot_id=values.get('id') or None,
        enabled=bool(values.get('enabled')),mode='enterprise',enterprise=target)
    return dict(message='企业通知机器人已保存：'+target['group_name']+' / '+target['robot_name'])


def search(b,values,profile=None):
    b.external();values=_values(values);client=EnterpriseClient()
    # 编辑沿用该机器人已确认的账号，新建只认当前组织或用户明确选择的账号。
    existing=None
    if values.get('id') and profile is None:
        existing=next((r for r in b.robots().list_robots() if r['id']==values['id']),None)
        if not existing or existing['mode']!='enterprise':raise ValueError('企业机器人不存在，请刷新列表')
        profile=existing['enterprise']['profile']
    result=resolve(client,values.get('group_name'),values.get('robot_name'),profile)
    if result['profiles']:
        token=b.review('robot-account',dict(values=values,profiles=result['profiles']))
        return dict(message='未找到唯一的当前组织账号，请选择本次使用的账号',forms=[A('robot-account','选择钉钉账号',[F('profile','钉钉账号',options=[(r['profile'],r['label']) for r in result['profiles']])],dict(token=token))])
    if existing and all(values.get(key)==existing['enterprise'][key] for key in ('group_name','robot_name')):
        # 编辑通知范围时保留既有目标身份；同名新群或机器人不能静默接管。
        if existing['enterprise'] in result['targets']:return _save(b,values,existing['enterprise'])
        result['exact']=False
    if result['exact']:return _save(b,values,result['targets'][0])
    token=b.review('robot-candidate',dict(values=values,targets=result['targets']))
    choices=[(str(i),r['group_name']+' / '+r['robot_name']+'（群 '+r['group_id'][-8:]+'）') for i,r in enumerate(result['targets'])]
    return dict(message='找到多个或相近名称，请选择实际接收群和机器人',forms=[A('robot-candidate','选择搜索结果',[F('choice','群和机器人',options=choices)],dict(token=token))])


def account(b,values):
    saved=b.reviewed(values.get('token'),'robot-account')
    if values.get('profile') not in {r['profile'] for r in saved['profiles']}:raise ValueError('请选择列表中的账号')
    return search(b,saved['values'],values['profile'])


def candidate(b,values):
    b.external();saved=b.reviewed(values.get('token'),'robot-candidate')
    try:choice=int(values.get('choice',''))
    except (TypeError,ValueError):raise ValueError('请选择搜索结果') from None
    if not 0<=choice<len(saved['targets']):raise ValueError('请选择列表中的搜索结果')
    target=verify_selection(EnterpriseClient(),saved['targets'][choice])
    return _save(b,saved['values'],target)
