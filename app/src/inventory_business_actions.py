"""业务操作显式白名单；预览和确认分离，批量写入沿用原子校验。"""
import json
import queue
import time
from pathlib import Path
from inventory_business_views import field as F, form as A, table as T
ACTIONS={}
def action(module,name):
    def register(fn):ACTIONS[(module,name)]=fn;return fn
    return register

def ids(value):
    return value if isinstance(value,list) else [s.strip() for s in str(value or '').replace('，',',').replace('\n',',').split(',') if s.strip()]

def document(body,name='记录.md'):return dict(document=str(body),filename=name)

@action('monitor','save')
def product_save(b,v):
    from inventory_panel import ProductDraft
    fields={key:v.get(key,'') for key in ('sku','goods_id','name','category','threshold','keywords','stock_basis','warehouse_id','warehouse_name')}
    fields['stock_basis']=fields['stock_basis'] or 'company_able'
    payload=ProductDraft(**fields,enabled=bool(v.get('enabled',True)),alerts={k:bool(v.get(k)) for k in ('inbound','pending_inbound','low','oversold','negative_stock','recovery')}).payload()
    existing=next((p for p in b.service.products() if p['sku']==payload['sku']),None)
    if existing and not v.get('editing'):raise ValueError('编码已关注，请从列表选择编辑')
    b.service.save_product(payload)
    return dict(message='产品配置已保存')

@action('monitor','catalog-add')
def product_catalog_add(b,v):
    from inventory_panel import ProductDraft
    # 只接受目录中的同一商品，不信任表单传入的名称或其他默认配置。
    goods_id=str(v.get('goods_id','')).strip()
    sku=str(v.get('sku','')).strip()
    if not goods_id or not sku:raise ValueError('请先选择 ERP 商品')
    row=next((r for r in b.service.catalog(goods_id,limit=100,mode='goods_id') if r['sku']==sku),None)
    if not row:raise ValueError('商品目录已变化，请重新搜索并选择')
    if any(p['sku']==sku or str(p.get('goods_id',''))==goods_id for p in b.service.products()):raise ValueError('该商品已关注，请从列表选择编辑')
    b.service.save_product(ProductDraft(sku=sku,goods_id=goods_id,name=row['name'],threshold=v.get('threshold','')).payload())
    return dict(message='产品配置已保存')


@action('monitor','remove-preview')
def product_remove_preview(b,v):
    selected=ids(v.get('skus'))
    rows=[p for p in b.service.products() if p['sku'] in selected]
    if not rows:raise ValueError('请选择需要移除的关注产品')
    token=b.review('remove',{p['sku']:p for p in rows})
    return dict(tables=[T('待移除关注',[('sku','SKU'),('name','商品')],rows)],forms=[A('remove-confirm','确认移除关注',[],dict(token=token),confirm='仅移除关注配置，保留历史记录；进行中的通知可能已经发送。')])

@action('monitor','remove-confirm')
def product_remove_confirm(b,v):
    expected=b.reviewed(v.get('token'),'remove')
    result=b.service.remove_products(list(expected),expected=expected)
    return dict(message=f'已移除 {len(result)} 个关注产品')

@action('monitor','warehouses')
def warehouses(b,v):
    b.external();b.service.request_warehouses(v.get('sku',''),v.get('goods_id',''))
    return dict(message='分库读取已请求，请稍后点击“查看分库”')

@action('monitor','warehouse-result')
def warehouse_result(b,v):
    result=b.service.warehouse_options(v.get('goods_id',''))
    return dict(message=result.get('error') or result['status'],tables=[T('分库库存',[('id','仓库编号'),('name','仓库'),('stock','库存'),('able','可销')],result.get('depots',[]))])

@action('monitor','import-preview')
def import_preview(b,v):
    from inventory_import import read_sheet,prepare_import
    plan=prepare_import(b.service,read_sheet(v['path']))
    return import_review_result(b,plan)

@action('monitor','import-confirm')
def import_confirm(b,v):
    from inventory_import import confirm_import
    count=confirm_import(b.service,b.reviewed(v.get('token'),'import'))
    return dict(message=f'已原子保存 {count} 个关注产品')

@action('monitor','template')
def import_template(b,v):
    from inventory_import import write_template
    path=b.service.root/'console_exports'/'关注产品导入模板.xlsx';path.parent.mkdir(exist_ok=True)
    write_template(path)
    return dict(message='模板已生成',download=b.review('download',path),filename=path.name)

@action('events','export')
def events_export(b,v):
    from inventory_delivery import format_events_detailed,format_inventory_report,format_events
    if v.get('kind')=='inventory':return document(format_inventory_report(b.service.status()),'库存快照.md')
    from inventory_business_views import event_rows
    rows,total=event_rows(b,v)
    if v.get('id'):rows=[r for r in rows if r['id']==v['id']]
    return document(format_events(rows[:6],b.service) if v.get('kind')=='preview' else format_events_detailed(rows,b.service),'库存提醒记录.md')

@action('settings','autostart')
def monitor_autostart(b,v):
    from monitor_portable_install import autostart_available,set_monitor_autostart
    if b.offline or not autostart_available():
        raise ValueError('请在 Windows 桌面程序中修改开机启动设置')
    enabled=v.get('enabled')
    if type(enabled) is not bool:
        raise ValueError('开机启动设置无效')
    try:
        actual=set_monitor_autostart(enabled,b.service.root)
    except OSError as exc:
        raise ValueError('无法修改 Windows 启动项，请检查当前用户权限') from exc
    if actual != enabled:
        raise ValueError('Windows 启动项状态与设置不一致，请刷新后检查')
    return dict(message='已开启登录后自动启动' if actual else '已关闭登录后自动启动')


@action('settings','source')
def source_save(b,v):
    b.external()
    # 设置页只接受 SQL，不允许旧页面继续切换到脚本采集。
    if v.get('mode','sql')!='sql':raise ValueError('库存数据来源仅支持 SQL 查询')
    b.service.configure_sql({k:(int(v.get(k,1433)) if k=='port' else v.get(k,'')) for k in ('server','port','database','user')},v.get('password',''),remember=bool(v.get('remember')))
    return dict(message='SQL 连接已应用')

@action('settings','catalog')
def catalog_refresh(b,v):
    b.external();b.service.sql_worker.refresh_catalog();return dict(message='已安排刷新完整商品目录')

@action('settings','interval')
def interval(b,v):b.service.set_interval(v['minutes']);return dict(message='监控周期已保存')

@action('settings','cache-preview')
def cache_preview(b,v):
    from inventory_cache_schedule import preview_action
    return preview_action(b,v)

@action('settings','cache-confirm')
def cache_confirm(b,v):
    from inventory_cache_schedule import execute_action
    return execute_action(b,v)

@action('settings','cache-settings')
def cache_settings(b,v):
    from inventory_cache_schedule import save_action
    return save_action(b,v)

@action('settings','workspace')
def workspace_settings_save(b,v):
    from inventory_workspace_config import save_workspace_settings
    save_workspace_settings(b.service,v)
    return dict(message='部门与工作区名称已保存')

@action('settings','ops-group-discover')
def ops_group_discover(b,v):
    from ops_group_discovery import discover
    return discover(b, v)


def register_ops_group_choice(kind):
    @action('settings','ops-group-' + kind)
    def handle(b,v):
        from ops_group_discovery import resolve_choice
        return resolve_choice(b, v, kind)


for _kind in ('account-choice','group-choice','robot-choice'):
    register_ops_group_choice(_kind)


@action('settings','ops-group-save')
def ops_group_save(b,v):
    from ops_settings import save_binding
    from ops_commands import COMMANDS
    selected = save_binding(b, v)
    return dict(message='群功能已保存并回读：' + ('、'.join(COMMANDS[k][0] for k in selected) or '已清空'))


@action('settings','ops-members-save')
def ops_members_save(b,v):
    from ops_settings import save_members
    return dict(message=f'权限已保存并回读：{save_members(b, v)} 名成员')


@action('settings','ops-members-template')
def ops_members_template(b,v):
    from ops_settings import template
    return template(b, v)


@action('settings','ops-members-import-preview')
def ops_members_import_preview(b,v):
    from ops_settings import preview_import
    return preview_import(b, v)


@action('settings','ops-members-import-confirm')
def ops_members_import_confirm(b,v):
    from ops_settings import confirm_import
    return dict(message=f'导入已保存并回读：当前群 {confirm_import(b, v)} 名成员')


@action('settings','robot-save')
def robot_save(b,v):
    if v.get('mode')=='enterprise':
        from notification_business import search
        return search(b,v)
    b.robots().save(v.get('name',''),v.get('webhook',''),ids(v.get('channels')),robot_id=v.get('id') or None,enabled=bool(v.get('enabled',False)),mode='webhook')
    return dict(message='通知机器人已保存')

@action('settings','robot-account')
def robot_account(b,v):
    from notification_business import account
    return account(b,v)

@action('settings','robot-candidate')
def robot_candidate(b,v):
    from notification_business import candidate
    return candidate(b,v)

@action('settings','robot-toggle')
def robot_toggle(b,v):b.robots().set_enabled(v['id'],bool(v['enabled']));return dict(message='机器人启停已保存')

@action('settings','robot-delete')
def robot_delete(b,v):b.robots().delete(v['id']);return dict(message='机器人已移除，历史回执保留')

@action('settings','enterprise-profiles')
def enterprise_profiles(b,v):
    b.external();from notification_enterprise import EnterpriseClient
    rows=EnterpriseClient().profiles()
    return dict(tables=[T('钉钉账号',[('label','账号'),('profile','标识')],rows)],forms=[A('enterprise-groups','查找群',[F('profile','账号',options=[(r['profile'],r['label']) for r in rows]),F('query','群名称')])])

@action('settings','enterprise-groups')
def enterprise_groups(b,v):
    b.external();from notification_enterprise import EnterpriseClient
    rows=EnterpriseClient().groups(v['profile'],v['query'])
    return dict(tables=[T('匹配群',[('group_name','群名'),('group_id','群标识')],rows)],forms=[A('enterprise-bots','读取群内机器人',[F('group_id','目标群',options=[(r['group_id'],r['group_name']) for r in rows])],dict(profile=v['profile'],groups=rows))])

@action('settings','enterprise-bots')
def enterprise_bots(b,v):
    b.external();from notification_enterprise import EnterpriseClient
    rows=EnterpriseClient().bots(v['profile'],v['group_id'])
    from inventory_business_views import robot_form
    group=next((r for r in v.get('groups',[]) if r['group_id']==v['group_id']),{})
    forms=[robot_form(dict(mode='enterprise',profile=v['profile'],group_id=v['group_id'],group_name=group.get('group_name',''),**r)) for r in rows]
    return dict(message='选择机器人后填写通知范围并保存',forms=forms)

@action('profit','preview')
def profit_preview(b,v):
    from inventory_shop_names import shop_label, shop_names
    result=b.profit().preview(v['path']);token=b.review('profit-bind',dict(path=v['path'],preview=result))
    names=shop_names(b.service.root)
    display=dict(result,shops='、'.join(shop_label(names,ident) for ident in result['shops']))
    return dict(tables=[T('配置范围',[('label','项目'),('value','内容')],[dict(label='店铺' if k=='shops' else k,value=x) for k,x in display.items()])],forms=[A('bind','确认绑定配置',[],dict(token=token),confirm='首次有效价格仅建立基准，不发送变化提醒。确认此来源、店铺和配置范围？')])

@action('profit','bind')
def profit_bind(b,v):
    plan=b.reviewed(v.get('token'),'profit-bind')
    if b.profit().preview(plan['path'])!=plan['preview']:raise ValueError('配置文件已变化，请重新预览')
    b.profit().bind(plan['path']);return dict(message='配置文件已绑定')

@action('profit','unbind')
def profit_unbind(b,v):b.profit().unbind();return dict(message='已解除绑定，保留历史结果')

@action('profit','schedule')
def profit_schedule(b,v):
    b.profit().set_schedule(v['times'],bool(v['enabled']));return dict(message='北京时间固定巡检时间已保存')

@action('profit','notify-preview')
def profit_notify_preview(b,v):
    preview=b.profit().notification_preview()
    token=b.review('profit-notify',preview)
    return dict(document='接收者：'+'、'.join(preview['recipients'])+'\n\n'+preview['message'],forms=[A('notify','启用价格提醒',[],dict(token=token,enabled=True),confirm='确认上述接收对象及消息内容，启用后达到阈值将发送通知。')])

@action('profit','notify')
def profit_notify(b,v):
    if v.get('enabled'):
        if b.profit().notification_preview()!=b.reviewed(v.get('token'),'profit-notify'):raise ValueError('接收对象或预览已变化，请重新确认')
    b.profit().set_notifications(bool(v['enabled']));return dict(message='价格通知开关已保存')

@action('profit','check')
def profit_check(b,v):
    b.external();ident=b.profit().check();return dict(message='检测完成：'+str(ident) if ident else '上一轮仍在执行')

@action('daily','settings')
def daily_settings(b,v):
    from inventory_daily import save_settings
    save_settings(b.service,v);return dict(message='货源日报时段已保存')

@action('daily','ai-save')
def ai_save(b,v):
    from inventory_ai import save
    save(b.service,v['url'],v['model'],v.get('key',''));return dict(message='AI 接入已保存，密钥不会回显')

@action('daily','ai-test')
def ai_test(b,v):
    b.external();from inventory_ai import call
    call(b.service,[dict(role='user',content='只回复 OK')]);return dict(message='AI 连接测试通过')

@action('daily','generate')
def daily_generate(b,v):
    b.external();from inventory_weekly import generate
    period=(v['start'],v['end']) if v.get('period')=='自定义日期' else v.get('period','本周')
    path=generate(b.service,period)
    return report_form(b,path)

def report_form(b,path):
    from inventory_report_delivery import destinations
    targets=destinations(b.robots());token=b.review('report',dict(path=path,targets=targets))
    return dict(message='报告已生成，请检查内容并手动选择接收机器人',document=path.read_text(encoding='utf-8'),forms=[
        A('report-save','编辑并保存报告',[F('body','报告正文','textarea')],dict(token=token,body=path.read_text(encoding='utf-8'))),
        A('report-send','审阅并发送报告',[F('body','报告正文','textarea'),F('recipients','接收机器人','multi',options=[(r['id'],r['name']) for r in targets if r['enabled']])],dict(token=token,body=path.read_text(encoding='utf-8')),confirm='已检查报告内容和所选接收机器人，确认发送；结果未知时不自动重发。')])

@action('daily','report-save')
def report_save(b,v):
    plan=b.reviewed(v.get('token'),'report');plan['path'].write_text(v['body'],encoding='utf-8')
    result=report_form(b,plan['path']);result['message']='报告已保存，可继续审阅并发送';return result

@action('daily','report-send')
def report_send(b,v):
    b.external();from inventory_report_delivery import send_reviewed
    plan=b.reviewed(v.get('token'),'report');selected=ids(v.get('recipients'))
    targets=[r for r in plan['targets'] if r['id'] in selected]
    result=send_reviewed(b.robots(),plan['path'],v['body'],targets)
    return dict(message='发送流程已结束，请按逐个机器人回执判断结果',tables=[T('投递回执',[('name','机器人'),('status','结果'),('sent_parts','已发送段数'),('total_parts','总段数')],result)])

@action('dingtalk','start')
def dingtalk_start(b,v):b.external();b.controller.start();return dict(message='已请求启动监听，以连接状态为准')
@action('dingtalk','stop')
def dingtalk_stop(b,v):b.external();b.controller.stop();return dict(message='已请求停止监听')

@action('dingtalk','profiles')
@action('dingtalk','login')
def dingtalk_profiles(b,v):
    b.external();rows=b.setup().login() if v.get('login') else b.setup().profiles()
    return dict(tables=[T('已登录账号',[('label','账号'),('key','标识')],rows)],forms=[A('resolve','核对监控对象',[F('profile','账号',options=[(r['key'],r['label']) for r in rows]),F('rows','监控来源','rows',columns=[('GroupName','群名'),('SenderName','发送人'),('Terms','关键词'),('AllowReopen','允许重开成本表')])],dict(rows=b.setup().initial_rows()))])

@action('dingtalk','resolve')
def dingtalk_resolve(b,v):
    b.external();from monitor_setup_service import ChoiceRequired
    choices=v.get('choices',{})
    def choose(title,candidates):
        if choices.get(title) in [x['id'] for x in candidates]:return next(x for x in candidates if x['id']==choices[title])
        raise ChoiceRequired(title,candidates)
    b.setup().check_account(v['profile'])
    try:rows=b.setup().resolve_sources(v['rows'],v['profile'],choose=choose)
    except ChoiceRequired as error:
        # 重名必须让用户选择具体结果，不能使用列表第一项。
        return dict(message=error.title,forms=[A('resolve-choice','选择匹配结果',[F('choice',error.title,options=[(r['id'],r['label']) for r in error.candidates])],dict(original=v,title=error.title))])
    token=b.review('sources',rows)
    return dict(tables=[T('已核对监控对象',[('GroupName','群名'),('SenderName','发送人'),('Terms','关键词')],rows)],forms=[A('save','保存监控来源',[F('webhook','接收 Webhook（留空保留）','password'),F('enabled','保存后启动监听','bool')],dict(token=token,enabled=False),confirm='确认来源及接收机器人；仅保存当前已核对的对象。')])

@action('dingtalk','resolve-choice')
def dingtalk_choice(b,v):
    original=v['original'];original['choices']={**original.get('choices',{}),v['title']:v['choice']}
    return dingtalk_resolve(b,original)

@action('dingtalk','save')
def dingtalk_save(b,v):
    b.external();b.setup().save(b.reviewed(v.get('token'),'sources'),v.get('webhook',''),b.controller,enable=bool(v.get('enabled')))
    return dict(message='监听来源已保存')

@action('douyin','add')
def shop_add(b,v):
    ident=b.douyin.store.add_store(v['name']);return dict(message='店铺已添加，请配置维护表和连接',select=ident)

@action('douyin','patrol')
def shop_patrol(b,v):
    selected=set(ids(v.get('shops')))
    for row in b.douyin.store.stores():b.douyin.store.select_patrol(row['id'],row['id'] in selected)
    return dict(message='参与巡检店铺已保存；未启动采集')

@action('douyin','check-all')
def shop_check_all(b,v):
    b.external();r=b.douyin.check_all();return dict(message=f"已排队 {r['queued']} 店，处理中 {r['busy']} 店；跳过："+'、'.join(r['skipped']))

@action('douyin','toggle')
def shop_toggle(b,v):
    b.external();r=b.douyin.toggle_selected();return dict(message=f"已提交{'启用' if r['enabled'] else '暂停'}：{r['queued']} 店；请查看各店状态")

@action('douyin','operation')
def shop_operation(b,v):
    allowed={'login','confirm','check','parts','online','enable','pause'}
    if v.get('operation') not in allowed:raise ValueError('未知店铺操作')
    b.external();b.douyin.submit(v['operation'],v['shop']);return dict(message='操作已排队，以店铺状态和实际采集时间为准')

@action('douyin','interval')
def shop_interval(b,v):
    minutes=int(v['minutes'])
    if not 5<=minutes<=1440:raise ValueError('巡检周期须为 5–1440 分钟')
    reply=queue.Queue(maxsize=1)
    b.douyin.submit(dict(kind='save_setting',operation='interval:'+str(minutes*60),reply=reply),v['shop']);return b.wait_reply(reply)

@action('douyin','remove')
def shop_remove(b,v):
    b.external();result=b.wait_reply(b.douyin.submit_remove(v['shop']));return dict(message='店铺已移出监控，在线同步状态请在店铺页查看')

@action('douyin','maintenance-preview')
def maintenance_preview(b,v):
    from douyin_maintenance import inspect_maintenance,KNOWN_SHOPS
    data=inspect_maintenance(v['path']);config=b.douyin.store.store(v['shop'])
    if KNOWN_SHOPS.get(config['name'],data['shop_id'])!=data['shop_id']:raise ValueError('维护表店铺编号与当前店铺不符')
    from inventory_shop_names import shop_label, shop_names
    workbook_shop=shop_label(shop_names(b.service.root),data['shop_id'])
    token=b.review('maintenance',dict(shop=v['shop'],data=data))
    return dict(message=f"当前店铺：{config['name']}；表内店铺：{workbook_shop}",forms=[A('maintenance-save','保存本店维护表',[F('sheet','配置工作表',options=[(s,s) for s in data['sheets']])],dict(token=token),confirm='读取本店已保存的配置工作表，不修改文件。')])

@action('douyin','maintenance-save')
def maintenance_save(b,v):
    plan=b.reviewed(v.get('token'),'maintenance');data=plan['data']
    if v['sheet'] not in data['sheets']:raise ValueError('请选择已检查的工作表')
    reply=queue.Queue(maxsize=1)
    operation='maintenance:'+json.dumps(dict(path=data['path'],sheet=v['sheet'],shop_id=data['shop_id']),ensure_ascii=False)
    b.douyin.submit(dict(kind='save_setting',operation=operation,reply=reply),plan['shop'])
    return b.wait_reply(reply)

@action('douyin','browser')
def browser_settings(b,v):
    b.external();reply=queue.Queue(maxsize=1)
    b.douyin.submit(dict(kind='browser_settings',value=dict(browser=v['browser'],profile=v.get('profile',''),mode=v['mode']),open=bool(v.get('open')),reply=reply),v['shop'])
    return b.wait_reply(reply)

@action('douyin','exclusions')
def exclusions(b,v):
    b.external();r=b.wait_reply(b.douyin.submit_exclusions(v['shop'],v.get('products',''),remove=bool(v.get('remove')),reason=v.get('reason','手动标记下架')))
    return dict(message=f"名单已保存：本店 {r['total']} 个排除链接")

@action('douyin','rules')
def low_rules(b,v):
    parts={}
    for row in v.get('parts',[]):
        key=str(row.get('goods_id','')).strip()
        if key:parts[key]=None if row.get('muted') else int(row.get('threshold',1))
    b.douyin.store.save_low_stock_rules(v['shop'],dict(default=int(v['default']),parts=parts))
    if not b.offline:b.douyin.submit('online',v['shop'])
    return dict(message='低库存预警规则已保存')

@action('douyin','restock')
def restock(b,v):
    from douyin_restock import brief
    data=next((r for r in b.douyin.store.restock_records(v['shop']) if r['id']==v.get('record')),None) if v.get('record') else b.douyin.store.restock_preview(v['shop'])
    payload=brief(data,b.douyin.store.store(v['shop']).get('online_url','')) if data else None
    return document(payload['markdown']['text'] if payload else '当前没有符合条件的补库存提醒。','补库存提醒.md')

@action('monitor','import-revise')
def import_revise(b,v):
    from inventory_import import prepare_import
    b.reviewed(v.get('token'),'import')
    return import_review_result(b,prepare_import(b.service,v['rows']))

def import_review_result(b,plan):
    token=b.review('import',plan)
    rows=[]
    for item in plan['rows']:
        row=dict(item);row['candidates_text']='；'.join(p['sku']+' · '+p['name'] for p in item['candidates']);rows.append(row)
    return dict(tables=[T('导入核对',[('line','行'),('goods_id','ERP 编号'),('name','商品'),('threshold','阈值'),('action','操作'),('problem','问题'),('candidates_text','可选商品')],rows)],forms=[
        A('import-revise','调整或排除导入行',[F('rows','导入行','rows',columns=[('goods_id','ERP 编号'),('name','商品名称'),('threshold','阈值'),('selected_sku','选定的完整 SKU'),('excluded','排除此行')])],dict(token=token,rows=plan['inputs'])),
        A('import-confirm','确认导入',[],dict(token=token),confirm='确认商品身份和阈值；未处理的异常行将阻止整批写入。')])

@action('douyin','export-file')
def export_file(b,v):
    path=Path(b.douyin.store.store(v['shop']).get('export_file') or '')
    if not path.is_file() or path.suffix.lower() not in ('.xlsx','.csv'):raise ValueError('当前店铺还没有可用的官方导出表')
    return dict(message='原始导出表已就绪',download=b.review('download',path),filename=path.name)

@action('douyin','browser-profiles')
def browser_profiles(b,v):
    b.external();import re,subprocess
    from douyin_export import cli_command
    result=subprocess.run(cli_command()+['profile','list'],capture_output=True,text=True,encoding='utf-8',timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise ValueError('扩展未连接，请检查 OpenCLI 扩展')
    rows=[dict(profile=s) for s in re.findall(r'^\s+([\w-]+)\s+—\s+connected',result.stdout,re.M)]
    return dict(tables=[T('已连接扩展',[('profile','扩展配置标识')],rows)])

@action('settings','recovery')
def recovery_settings(b,v):
    # 与原界面共用自动重登开关，不读取登录资料。
    with b.service.db() as db:
        b.service.put(db,'erp_native_enabled',bool(v.get('enabled')))
        if v.get('enabled'):b.service.put(db,'erp_native_state',{})
    return dict(message='自动重登设置已保存')

@action('profit','view-changes')
def view_price_changes(b,v):
    # 手动查看只检测成本，不发送通知、不改变原定时提醒的累计基准。
    if not (b.offline and getattr(b,'preview_prices',False)):b.external()
    ident=b.profit().check(allow_notifications=False)
    if not ident:raise ValueError('上一轮价格检测仍在执行，请稍后查看')
    from inventory_price_changes import changes
    current=changes(b.service,dict(run_id=ident))
    label='检测完成' if current.get('run_status')=='completed' else '检测未完成，请查看来源状态'
    return dict(message=f"{label}：{current['changed_count']} 个配件价格变化，{current['unknown_count']} 个待核对。",select='changes')


@action('profit','maintenance-preview')
def price_maintenance_preview(b,v):
    # 复用已配置的维护表也先预览，不能静默扩大价格巡检范围。
    from price_maintenance import inspect_sources, saved_sources
    sources=saved_sources(b.douyin.store) if v.get('reuse') else v.get('sources')
    from inventory_shop_names import named_shop_rows, shop_names
    preview=inspect_sources(sources)
    names=shop_names(b.service.root)
    result=dict(message=f"维护表：{preview['count']} 套配置 / {preview['goodsCount']} 个配件 ID，跳过 {len(preview['issues'])} 项异常行。只读已保存内容。",
        tables=[T('维护表范围',[('path','文件'),('sheet','工作表'),('shop_name','店铺'),('count','配置数')],named_shop_rows(preview['files'],names)),
                T('跳过的异常行',[('path','文件'),('sheet','工作表'),('row','行'),('reason','问题')],preview['issues'])])
    if preview['complete']:
        token=b.review('price-maintenance',preview)
        result['forms']=[A('maintenance-bind','确认绑定维护表',values=dict(token=token),confirm='确认店铺和配件范围；首次有效采集只建立价格基准。')]
    return result


@action('profit','maintenance-bind')
def price_maintenance_bind(b,v):
    from price_maintenance import inspect_sources
    plan=b.reviewed(v.get('token'),'price-maintenance')
    if inspect_sources(plan['sources'])!=plan:raise ValueError('维护表已变化，请重新预览')
    b.profit().bind_maintenance(plan['sources'],expected_preview=plan)
    return dict(message='主机维护表已绑定，每轮巡检重新读取配件 ID 和配置关系')


@action('order-settings','backend-auto')
def order_backend_auto(b,v):
    from order_business import auto_backend
    return auto_backend(b,v)

@action('order-settings','backend-save')
def order_backend_save(b,v):
    from order_business import save_backend
    return save_backend(b,v)

@action('order-settings','backend-deploy')
def order_backend_deploy(b,v):
    from order_business import deploy_backend
    return deploy_backend(b,v)

@action('order-settings','robot-search')
def order_robot_search(b,v):
    from order_business import search_robot
    return search_robot(b,v)

@action('order-settings','robot-select')
def order_robot_select(b,v):
    from order_business import select_robot
    return select_robot(b,v)
