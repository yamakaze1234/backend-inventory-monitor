"""发货监控工作台：只读结果，操作共用既有执行器。"""
from contextlib import closing
import json
import sqlite3
from pathlib import Path
from inventory_business_views import A,F,S,T
from order_business import adapter
from inventory_schedule_view import shipping_plan, notification_state, apply_schedule


def view(b,q):
    state=notification_state(b,adapter(b).configuration())
    card=shipping_plan(state,False)
    if isinstance(getattr(b.service,'root',None),(str,Path)):
        apply_schedule(b.service.root,'shipping',card)
    card['text']='；'.join(str(card.get(k) or '') for k in ('summary','status','note'))
    if state.get('deployed') and state.get('shippingReady') and state.get('shippingBackendCurrent') is not False:
        card['forms'].append(A('interval','设置监控周期',[F('minutes','监控周期（分钟）','number')],
            dict(minutes=state.get('shipping',{}).get('intervalMinutes') or 120),module='shipping'))
    auth=S('sf-auth','顺丰揽收查询认证')
    auth['text']='先配置订单匹配后端，再填写顺丰生产接口凭据。'
    if state.get('configured') and state.get('state_root'):
        from shipping_sf_config import summary
        try:
            values=summary(state['state_root'])
            auth['text']='已保存校验码；留空保留。' if values.pop('configured') else '尚未保存生产校验码。'
        except ValueError:
            values={}
            auth['text']='当前用户无法读取凭据，请重新填写顾客编码和校验码。'
        auth['text']+=' 仅保存认证，不启动巡检或验证接口权限；SDK 模式需后端已安装对应依赖。'
        auth['forms']=[A('sf-auth-save','保存顺丰认证',[
            F('provider','接口方式',options=[('sf-open','顺丰开放平台'),('sf-sdk','顺丰 SDK')]),
            F('partnerId','顾客编码'),F('checkWord','生产校验码（留空保留）','password')],values,module='shipping')]
    latest=S('latest','最近巡检明细')
    root=state.get('state_root')
    path=Path(root)/'jobs.sqlite3' if root else None
    if path and path.is_file():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute("SELECT state,delivery,result FROM jobs WHERE kind='shipping' ORDER BY created DESC LIMIT 1").fetchone()
        if row:
            report=json.loads(row[2]) if row[2] else {}
            latest['text']='任务：'+row[0]+'；通知：'+row[1]+'；检查时间：'+str(report.get('checkedAt',''))
            latest['tables']=[T('待处理订单',[('shop','店铺'),('platformNo','网店单号'),('code','ERP 单号'),
                ('level','紧急程度'),('deadline','发货截止'),('status','订单状态')],report.get('orders',[]))]
            latest['text']+='\n'+'\n'.join(report.get('errors',[]))
            latest['links']=[dict(label=label,url=report[key]['url']) for key,label in
                (('onlineSync','发货在线明细'),('shortageSync','缺货配件明细'))
                if report.get(key,{}).get('complete') and str(report.get(key,{}).get('url','')).startswith('https://')]
        else:latest['text']='尚未执行发货巡检'
    return dict(module='shipping',query=q,filters=[],sections=[card,auth,latest])
