"""业务页描述：仅包含面向用户的字段和安全状态，使用同一套 ReUI 组件渲染。"""
import time

def field(key,label,kind='text',options=None,**extra):
    return dict(key=key,label=label,kind='select' if options is not None and kind=='text' else kind,options=[dict(value=str(v),label=str(t)) for v,t in (options or [])],**extra)
def form(action,title,fields=None,values=None,confirm='',**extra):
    return dict(action=action,title=title,fields=fields or [],values=values or {},confirm=confirm,**extra)
def table(title,columns,rows,**extra):
    return dict(title=title,columns=[dict(key=k,label=v) for k,v in columns],rows=rows,**extra)
def section(key,title,**extra):return dict(id=key,title=title,forms=[],tables=[],**extra)
F,A,T,S=field,form,table,section
CHANNELS=[('inventory','库存提醒'),('douyin_inventory','抖店补库存（自动群发送关闭）'),('douyin_patrol_failure','巡检失败提醒'),('daily','货源日报'),('messages','货源即时转发'),('profit','ERP 单价提醒')]

def robot_form(row=None):
    row=row or {}
    values={**dict(mode='enterprise',enabled=False,channels=['inventory']),**{k:v for k,v in row.items() if k!='enterprise'}}
    values.update(row.get('enterprise') or {})
    fields=[F('name','备注名称',when=dict(mode='webhook')),F('mode','机器人类型',options=[('webhook','Webhook 机器人'),('enterprise','企业机器人')]),
        F('webhook','Webhook（留空保留原地址）','password',when=dict(mode='webhook')),F('group_name','目标群名称',when=dict(mode='enterprise')),F('robot_name','企业机器人名称',when=dict(mode='enterprise')),F('channels','通知类型','multi',options=CHANNELS),F('enabled','启用机器人','bool')]
    return A('robot-save','编辑机器人' if row.get('id') else '添加机器人',fields,values,module='settings')

def catalog_add_form(row=None):
    # 已选中的目录商品直接设置阈值；普通入口在弹窗内搜索。
    row=row or {}
    return A('catalog-add','关注此商品' if row else '添加产品',values=dict(threshold=5,sku=row.get('sku',''),goods_id=row.get('goods_id',''),name=row.get('name','')))


def product_form(row=None):
    row=row or {}
    values=dict(category='内存',threshold=5,enabled=True,stock_basis='company_able',**{k:v for k,v in row.items() if k not in ('category','threshold','enabled','stock_basis','alerts')})
    values.update({k:row[k] for k in ('category','threshold','enabled','stock_basis') if k in row})
    values.update({k:row.get('alerts',{}).get(k,True) for k in ('inbound','pending_inbound','low','oversold','negative_stock','recovery')})
    values['editing']=bool(row)
    return A('save','编辑产品' if row else '添加产品',[F('sku','SKU 编码',readonly=bool(row)),F('goods_id','ERP 商品编号'),F('name','商品名称'),F('category','分类',options=[(v,v) for v in ('RTX 5070 及以上显卡','其余显卡','主板','CPU','内存','其他')]),F('threshold','预警阈值','number'),F('keywords','货源消息关键词'),F('enabled','启用监控','bool'),F('stock_basis','库存口径',options=[('company_able','公司大库可销'),('warehouse_stock','指定仓库库存')]),F('warehouse_id','仓库编号'),F('warehouse_name','仓库名称'),*[F(k,t,'bool') for k,t in [('inbound','入库 / 库存增加'),('pending_inbound','待入库'),('low','低库存'),('oversold','超售'),('negative_stock','负库存'),('recovery','风险恢复')]]],values)

def event_rows(b,q):
    from inventory_event_ui import KINDS
    period=q.get('period','全部')
    since=0
    if period=='今天':
        local=time.localtime();since=time.mktime((local.tm_year,local.tm_mon,local.tm_mday,0,0,0,0,0,-1))
    elif period in ('近24小时','近7天'):since=time.time()-{'近24小时':86400,'近7天':604800}[period]
    return b.service.preview_events(kinds=KINDS.get(q.get('type','全部类型')),warehouse=q.get('warehouse',''),query=q.get('query',''),since=since)

def matches_product(row,q):
    """顶部筛选统一作用于关注产品、ERP目录和检查明细。"""
    query=str(q.get('query','')).strip();mode=q.get('mode','all')
    if mode not in ('all','name','goods_id'):raise ValueError('未知搜索模式')
    if mode=='goods_id':return not query or str(row.get('goods_id',''))==query
    haystack=str(row.get('name','')) if mode=='name' else ' '.join(str(row.get(k,'')) for k in ('name','sku','goods_id'))
    return all(term in haystack.casefold() for term in query.casefold().split())


def build_view(b,module,q):
    sections=[];filters=[]
    if module in ('environment-settings','cache-settings'):
        from inventory_settings_view import view
        return view(b,module,q)
    if module in ('engine-settings','daily-settings'):
        # 独立导航沿用同一业务操作，防止形成两套配置保存路径。
        if module=='engine-settings':
            from order_business import view
            sections=view(b,q)['sections'][:1]
        else:
            from daily_report_business import view
            sections=[view(b.service.root)]
        for section in sections:
            for form in section['forms']:form['module']='order-settings'
        return dict(module=module,filters=[],query=q,sections=sections)
    if module=='shipping':
        from shipping_business import view
        return view(b,q)
    if module=='schedules':
        from inventory_schedule_view import view
        return view(b,q)
    if module=='order-settings':
        from order_business import view
        result=view(b,q)
        result['sections']=result['sections'][1:2]
        return result
    if module=='monitor':
        all_products=b.service.products()
        rows=[p for p in all_products if matches_product(p,q)]
        for p in rows:p['_forms']=[product_form(p)]
        selected=[(p['sku'],p['name']+' · '+p['sku']) for p in rows]
        main=S('products','关注产品');main['forms']=[catalog_add_form(),A('remove-preview','批量移除关注',[F('skus','选择产品','multi',options=selected)]),A('import-preview','导入表格',[F('path','Excel / CSV 文件','file')]),A('template','下载导入模板')]
        main['description']=f'当前筛选 {len(rows)} / {len(all_products)} 件关注商品'
        main['tables']=[T('已关注产品',[('name','商品'),('sku','SKU'),('threshold','提醒阈值'),('enabled','监控'),('warehouse_name','仓库')],rows)]
        catalog=b.service.catalog(q.get('query',''),limit=500,mode=q.get('mode','all'))
        catalog=[dict(r,_forms=[catalog_add_form(r)]) for r in catalog]
        search=S('catalog','搜索 ERP 商品');search['tables']=[T('ERP 目录（最多 500 项）',[('name','商品'),('goods_id','ERP 编号'),('sku','SKU'),('able','可销')],catalog)]
        search['forms']=[A('warehouses','请求分库列表',[F('sku','SKU'),F('goods_id','ERP 商品编号')]),A('warehouse-result','查看分库',[F('goods_id','ERP 商品编号')])]
        filters=[F('query','搜索商品或编号'),F('mode','匹配方式',options=[('all','关键词'),('goods_id','完整 ERP 编号'),('name','名称')])]
        # 保留逐商品检查覆盖情况，明确区分本轮与上轮结果。
        status=b.service.status();coverage=status.get('coverage',{});labels={'success':'已更新','isolated':'已隔离','missing':'商品未返回','warehouse_missing':'库房待核对'}
        health=S('coverage','本轮检查明细');health['description']='本轮结果' if status.get('coverage_current',True) else '上轮结果，请结合当前监控状态核对'
        if status.get('connection')!='online':health['description']+=' · 当前数据尚未确认'
        last={p['sku']:p.get('observed_at',0) for p in status.get('products',[])}
        checks=[]
        for item in coverage.get('items',[]):
            if not matches_product(item,q):continue
            stamp=coverage.get('checked_at',0) if item['state']=='success' else last.get(item['sku'],0)
            checks.append(dict(item,state=labels.get(item['state'],item['state']),last=time.strftime('%m-%d %H:%M:%S',time.localtime(stamp)) if stamp else '尚无有效库存'))
        health['tables']=[T('逐商品覆盖情况',[('name','商品'),('goods_id','ERP 编号'),('warehouse','库房'),('state','检查结果'),('last','上次有效检查'),('reason','原因')],checks)]
        sections=[main,search,health]
    elif module=='events':
        rows,total=event_rows(b,q)
        from inventory_message_view import LABELS,event_change
        for r in rows:
            r['kind_label']=LABELS.get(r['kind'],r['kind']);r['change']=event_change(r)
            r['_forms']=[A('export','查看事件详情',[],dict(q,id=r['id']))]
        section=S('history','库存提醒记录');section['description']=f'匹配 {total} 条，显示最近 {len(rows)} 条；查看和导出不会重新发送。'
        section['tables']=[T('提醒记录',[('name','商品'),('kind_label','事件'),('change','变化'),('warehouse','仓库'),('delivery_status','投递状态')],rows)]
        section['forms']=[A('export','导出当前筛选',[],dict(q)),A('export','消息预览',[],dict(q,kind='preview')),A('export','导出库存快照',[],dict(kind='inventory'))]
        from inventory_event_ui import KINDS
        filters=[F('query','商品 / 编号'),F('period','时间',options=[(x,x) for x in ('全部','今天','近24小时','近7天')]),F('type','事件类型',options=[(x,x) for x in KINDS]),F('warehouse','仓库（留空全部）')]
        sections=[section]
    elif module=='settings':
        with b.service.db() as db:
            cfg=b.service.get(db,'sql_connection',{});remember=b.service.get(db,'sql_remember',False)
        source=S('source','库存数据来源');source['forms']=[A('source','保存 SQL 连接',[F('server','服务器'),F('port','端口','number'),F('database','数据库'),F('user','查询账号'),F('password','密码（留空使用已保存密码）','password'),F('remember','加密保存密码','bool')],dict({k:cfg.get(k,1433 if k=='port' else 'ERP' if k=='database' else '') for k in ('server','port','database','user')},remember=remember)),A('catalog','刷新完整 ERP 目录')]
        source['planLink']='inventory'
        source['text']='使用 SQL 查询 ERP 库存。旧来源配置可通过保存 SQL 连接切换。'
        with b.service.db() as db:
            network=b.service.get(db,'sql_network_override',{})
            address=network.get('address','') if network.get('scope')==b.service.get(db,'scope') else ''
        source['forms'].append(A('sql-network','设置本机直连地址',[F('address','直连 IP（留空使用原服务器地址）')],dict(address=address),
            description='用于域名解析异常；保存前验证连接和库存视图。保留原账号、数据库、库存基线及其他工具配置；不设置代理。'))
        source['text']+=' 当前连接地址：'+(address or str(cfg.get('server') or '未配置'))
        robots=S('robots','通知机器人');robots['description']='企业机器人只需群名称和机器人名称，账号与标识自动查询；重名时选择候选。这里仅管理通知，订单匹配机器人单独配置。';rows=b.robots().list_robots()
        for row in rows:row['_forms']=[robot_form(row),A('robot-toggle','暂停' if row['enabled'] else '启用',[],dict(id=row['id'],enabled=not row['enabled']),confirm='确认调整该机器人的通知开关？'),A('robot-delete','移除机器人',[],dict(id=row['id']),confirm='移除机器人，历史回执保留。')]
        robots['forms']=[robot_form()];robots['tables']=[T('机器人列表',[('name','名称'),('mode','类型'),('enabled','启用'),('channels','通知范围')],rows),T('近期回执',[('name','机器人'),('status','状态'),('channel','类型')],b.robots().recent_receipts())]
        from inventory_cache_schedule import settings_section
        from inventory_workspace_config import workspace_settings
        workspace=workspace_settings(b.service)
        department=S('workspace','部门与工作区');department['description']='新安装使用独立数据目录；现有工作区继续保留原有配置。未配置的业务模块不会自动接收任务。'
        department['forms']=[A('workspace','保存工作区信息',[F('department','部门名称'),F('name','工作区名称')],workspace)]
        department['text']='当前数据目录：'+workspace['data_dir']
        from monitor_portable_install import autostart_available,autostart_enabled
        startup=S('startup','启动与运行',description='登录 Windows 后在系统托盘启动后台库存监控。')
        available=not b.offline and autostart_available()
        try:
            enabled=autostart_enabled() if available else False
        except OSError:
            available,enabled=False,False
            startup['text']='无法读取 Windows 启动项，请检查当前用户权限。'
        startup['autostart']=dict(available=available,enabled=enabled)
        from ops_settings import section as ops_section
        sections=[startup,department,source,robots,ops_section(b),settings_section(b)]
    elif module=='profit':
        from inventory_shop_names import named_shop_rows, shop_label, shop_names
        names=shop_names(b.service.root)
        state=b.profit().status();main=S('scope','价格监控配置')
        # 价格监控仅展示维护表入口；旧快照数据仍保留以便历史查询。
        main['forms']=[A('unbind','解除绑定',confirm='停止价格定时检测及通知，保留历史记录。'),A('notify','关闭价格通知',values=dict(enabled=False)) if state['notify'] else A('notify-preview','预览并启用价格通知')]
        main['planLink']='price'
        from price_maintenance import saved_sources
        bindings=state['maintenanceSources'] or saved_sources(b.douyin.store) or [dict(path='',sheet='',shop_id='')]
        main['forms']=[A('maintenance-preview','预览已有主机维护表',values=dict(reuse=True)),A('maintenance-preview','选择主机维护表',[F('sources','维护表清单','rows',columns=[('path','文件完整路径'),('sheet','工作表'),('shop_id','ERP 店铺编号')])],dict(sources=bindings)),*main['forms']]
        main['tables']=[T('已绑定维护表',[('path','文件'),('sheet','工作表'),('shop_name','店铺')],named_shop_rows(state['maintenanceSources'],names))]
        main['text']=f"已绑定 {state['count']} 套配置 / {state['goodsCount']} 个 ERP 商品；跳过 {state.get('skippedRows',0)} 项异常行；累计涨跌达到 100 元提醒。涨幅 ≥5%（含从零上涨）/ 上涨 <5% / 降价分别展示。来源状态：{state['sourceError'] or state['lastError'] or '正常'}"
        from price_schedule import TZ
        from datetime import datetime
        next_time=datetime.fromtimestamp(state['nextCheck'],TZ).strftime('%Y-%m-%d %H:%M') if state['enabled'] and state['nextCheck'] else '未启用 / 待执行'
        main['description']='下次巡检（北京时间）：'+next_time+'；每轮重新读取已保存的维护表。'
        results=S('results','检测结果');rows=state['lastResults']
        for r in rows:
            r['price']='—' if not r['valid'] else f"{r['unit_cents']/100:.2f}"
            r['delta']='—' if r['anchor_delta_cents'] is None else f"{r['anchor_delta_cents']/100:.2f}"
            r['_details']=[dict(label='关联配置',value=' / '.join([shop_label(names,shop),*map(str,ref)])) for shop,*ref in r.get('refs',[])]
        results['tables']=[T('配件价格变化',[('goods_id','ERP 编号'),('name','配件'),('price','当前单价'),('delta','累计变化'),('affectedConfigs','影响配置'),('reason','采集说明')],rows),T('近期异常事件',[('name','商品'),('status','状态'),('message','说明')],state['events'])]
        from inventory_price_changes import changes,result_tables
        snapshot=changes(b.service,q,shop_names=names)
        changes_section=S('changes','查看价格变动');changes_section['description']=snapshot['message']
        changes_section['forms']=[A('view-changes','一键查看价格变动',immediate=True)]
        changes_section['tables']=result_tables(snapshot,names)
        if snapshot.get('checked_at'):changes_section['text']='检测时间：'+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(snapshot['checked_at']))
        filters=[F('comparison','对比方式',options=[('batch','与上次检测比较'),('day','该日批次与前一日比较')]),F('direction','价格变化',options=[('all','全部'),('up','全部涨价'),('high','上涨 ≥5% / 从零上涨'),('small_up','上涨 <5%'),('down','降价'),('same','未变化'),('baseline','建立基准'),('unknown','待核对')]),F('query','配件 / 链接 / 配置 / 店铺'),F('run_id','历史批次',options=[('','最近批次')]+[(r['id'],time.strftime('%m-%d %H:%M:%S',time.localtime(r['started']))) for r in snapshot['batches']])]
        sections=[changes_section,main,results]
    elif module=='daily':
        from inventory_ai import config
        cfg=config(b.service)
        with b.service.db() as db:last=b.service.get(db,'daily_last',{})
        weekly=S('weekly','货源周报');weekly['forms']=[A('generate','生成货源周报',[F('period','统计范围',options=[(x,x) for x in ('本周','上周','自定义日期')]),F('start','开始日期','date'),F('end','结束日期','date')],dict(period='本周'))]
        weekly['description']='生成后在页面审阅、编辑和下载，再手动选择接收机器人。'
        schedule=S('schedule','定时日报',planLink='daily');schedule['text']='最近日报：'+str(last.get('status','尚无'))+' '+str(last.get('reason',''))
        ai=S('ai','AI 接入');ai['forms']=[A('ai-save','保存 AI 设置',[F('url','兼容接口 HTTPS 地址'),F('model','模型'),F('key','API Key（留空保留）','password')],{k:cfg.get(k,'') for k in ('url','model')}),A('ai-test','测试已保存的 AI 连接')]
        receivers=S('receivers','接收机器人');receivers['forms']=[robot_form()];receivers['tables']=[T('接收机器人',[('name','名称'),('enabled','启用'),('channels','通知范围')],[dict(r,_forms=[robot_form(r)]) for r in b.robots().list_robots()])]
        sections=[weekly,schedule,ai,receivers]
    elif module=='dingtalk':
        state=b.controller.status() if b.controller else dict(state='paused',ready=0,sources=[])
        from monitor_app_control import read_json
        rows=read_json(b.controller.root/'monitor_sources.json',[]) if b.controller else []
        rows=[{k:r.get(k,'') for k in ('GroupName','SenderName','Terms','AllowReopen')} for r in rows]
        main=S('listener','消息监听');main['text']=f"监听状态：{state['state']} · 已连接 {state.get('ready',0)} 个来源"
        main['forms']=[A('start','启动监听'),A('stop','停止监听'),A('profiles','配置监控对象'),A('login','登录钉钉账号',values=dict(login=True))]
        main['tables']=[T('监控来源',[('GroupName','群名'),('SenderName','发送人'),('Terms','关键词'),('AllowReopen','允许重开成本表')],rows)]
        # 货源汇报归入钉钉货源监控，原动作仍交给 daily 服务，避免功能丢失。
        daily=build_view(b,'daily',{})
        for item in daily['sections']:
            for action in item['forms']:action.setdefault('module','daily')
            for listing in item['tables']:
                for row in listing['rows']:
                    for action in row.get('_forms',[]):action.setdefault('module','daily')
        sections=[main,*daily['sections']]
    elif module=='douyin':return douyin_view(b,q)
    return dict(module=module,filters=filters,query=q,sections=sections)


def douyin_view(b,q):
    stores=b.douyin.store.stores();shop=q.get('shop') or (stores[0]['id'] if stores else '')
    options=[(s['id'],s['name']) for s in stores]
    base=dict(shop=shop)
    main=S('stock','整机库存');main['forms']=[A('check-all','检查参与巡检店铺'),A('toggle','启动 / 暂停所选监控'),A('export-file','下载本店原始导出表',values=base),A('patrol','选择参与巡检店铺',[F('shops','店铺','multi',options=options)],dict(shops=[s['id'] for s in stores if s.get('patrol_selected')]))]
    from douyin_restock import stamp
    from douyin_inventory import PATROL_HISTORY_LIMIT
    # 运行记录覆盖所有店铺，不受当前店铺或 SKU 筛选条件影响。
    runs = [dict(id=str(r['id']), finished=stamp(r['finished']), duration=f"{max(0, round(r['finished']-r['started']))} 秒",
                 shop=r['shop'], trigger=r['trigger'], status=r['outcome'],
                 detail=r['detail'], delivery=r['delivery']) for r in b.douyin.store.patrol_history()]
    main['tables'].append(T(f'最近运行记录（最多 {PATROL_HISTORY_LIMIT} 条）',
        [('finished','结束时间'),('duration','用时'),('shop','店铺'),('trigger','触发方式'),
         ('status','巡检结果'),('delivery','钉钉提醒'),('detail','运行说明')],runs,compact=True,
        widths=dict(finished=132,duration=55,shop=116,trigger=72,status=76,delivery=170,detail=200)))
    for s in stores:s['activity']=b.douyin.activity(s['id']) or s['status']
    shops=S('shops','店铺管理');shops['forms']=[A('add','添加店铺',[F('name','与抖店后台一致的店名')])]
    shops['tables']=[T('店铺状态',[('name','店铺'),('activity','状态'),('enabled','定时监控'),('patrol_selected','参与巡检')],stores)]
    filters=[F('shop','当前店铺',options=options),F('query','搜索链接 / SKU'),F('filter','库存筛选',options=[(x,x) for x in ('全部库存','低库存预警','库存为零','ERP 缺货','ERP 待核对','有库存','库存未知')])]
    if not shop:
        main['forms']=[]
        return dict(module='douyin',sections=[shops,main],filters=filters,query=q)
    config=b.douyin.store.store(shop)
    from douyin_scope import machine_rows
    from douyin_low_stock import is_low
    rows=machine_rows(b.douyin.store.snapshots(shop));rules=b.douyin.store.low_stock_rules(shop)
    needle=q.get('query','').casefold();choice=q.get('filter','全部库存')
    rows=[r for r in rows if needle in ' '.join(str(r.get(k,'')) for k in ('product_name','sku_name','product_id','sku_id')).casefold() and
        (choice=='全部库存' or choice=='低库存预警' and is_low(r,rules) or choice=='库存为零' and r['quantity']==0 or choice=='ERP 缺货' and r.get('erp',{}).get('state')=='缺货' or choice=='ERP 待核对' and r.get('erp',{}).get('state','待核对') in ('待核对','未核对') or choice=='有库存' and (r['quantity'] or 0)>0 or choice=='库存未知' and r['quantity'] is None)]
    for r in rows:
        erp=r.get('erp',{});r['erp_state']=erp.get('state','未核对')
        r['id']=r['product_id']+':'+r['sku_id'];r['url']='https://haohuo.jinritemai.com/views/product/item2?id='+r['product_id']
        r['_details']=[dict(label='SKU 编号',value=r['sku_id']),dict(label='售卖状态',value=r.get('stock_state','未核对')),dict(label='维护表',value=erp.get('workbook','未配置')),dict(label='工作表 / 行',value=str(erp.get('sheet',''))+' / '+str(erp.get('row',''))),*[dict(label=p.get('name',p.get('goods_id','配件')),value=f"编号 {p.get('goods_id')} · 每台 {p.get('required')} · 可销 {p.get('available')} · 缺 {p.get('shortage','—')} · 待入 {p.get('incoming','—')}") for p in erp.get('parts',[])+erp.get('shortages',[])],*[dict(label='待核对',value=x) for x in erp.get('unknown',[])]]
    from douyin_online import status as online_status
    main['text']=(b.douyin.activity(shop) or config['status'])+'\n'+str(online_status(b.douyin.store.root,shop).get('message',''));main['tables'].append(T('整机商品与 SKU',[('product_name','链接商品'),('sku_name','SKU'),('quantity','商家库存'),('erp_state','ERP 配件'),('stock_state','售卖状态')],rows))
    connection=S('connection','本店连接与维护表');connection['forms']=[A('browser-profiles','刷新已有扩展连接'),A('browser','保存浏览器设置',[F('browser','浏览器',options=[('chrome','Chrome'),('edge','Edge')]),F('mode','连接方式',options=[('managed','独立店铺浏览器'),('cws','CWS 独立店铺配置'),('extension','已有 OpenCLI 扩展')]),F('profile','扩展配置标识'),F('open','保存后打开本店浏览器','bool')],dict(base,browser=config['browser'],mode=config.get('connection_mode','managed'),profile=config.get('browser_profile',''),open=False)),A('operation','校验本店连接',values=dict(base,operation='login')),A('operation','确认店铺并建立基线',values=dict(base,operation='confirm'),confirm='确认当前浏览器登录的店铺是“'+config['name']+'”？首次导出建立基线。'),A('maintenance-preview','选择并检查维护表',[F('path','本店维护表','file')],dict(base,path=config['maintenance_path'])),A('operation','核对 ERP 配件',values=dict(base,operation='parts')),A('remove','移除本店监控',values=base,confirm='移除当前店铺监控并同步移除在线提醒，保留历史导出。')]
    connection['planLink']='shops'
    low=S('rules','低库存预警');low['forms']=[A('rules','保存预警规则',[F('default','默认库存阈值','number'),F('parts','配件例外','rows',columns=[('goods_id','ERP 配件编号'),('threshold','阈值'),('muted','不预警')])],dict(base,default=rules['default'],parts=[dict(goods_id=k,threshold=v if v is not None else 1,muted=v is None) for k,v in rules['parts'].items()]))]
    excluded=S('excluded','人工排除链接');excluded['tables']=[T('已排除链接',[('product_id','商品 ID'),('product_name','商品'),('reason','备注')],b.douyin.store.excluded_products(shop))];excluded['forms']=[A('exclusions','更新排除名单',[F('products','商品链接或 ID（每行一个）','textarea'),F('reason','备注'),F('remove','从排除名单移出','bool')],dict(base,reason='手动标记下架',remove=False))]
    records=b.douyin.store.restock_records(shop)
    restock=S('restock','补库存提醒');restock['forms']=[A('restock','预览补库存清单',[F('record','查看时间',options=[('','当前清单')]+[(r['id'],str(r['detected_at'])) for r in records])],base),A('operation','同步在线明细',values=dict(base,operation='online'))]
    if config.get('online_url'):restock['links']=[dict(label='打开在线明细',url=config['online_url'])]
    history=S('history','库存变化');history['tables']=[T('历史记录',[('product_name','商品'),('sku_name','SKU'),('previous','上次库存'),('quantity','当前库存'),('kind','变化'),('status','通知状态')],[r for r in machine_rows(b.douyin.store.events()) if r['store_id']==shop])]
    return dict(module='douyin',filters=filters,query=dict(q,shop=shop),sections=[main,restock,history,shops,connection,low,excluded])
