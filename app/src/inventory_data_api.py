"""机器人只读数据契约：固定数据集、字段白名单、稳定分页，不执行 ERP 写入。"""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

LIMIT = 100000
TZ = timezone(timedelta(hours=8))
# 字段名既是返回白名单，也是筛选和排序白名单；内部连接信息永远不进入查询结果。
DATASETS = {
    'inventory': ('配件库存快照', 'sku name goods_id warehouse_name monitor_qty able stock purchase threshold enabled risk observed_at', 'observed_at'),
    'products': ('关注产品配置', 'sku name goods_id category threshold enabled stock_basis warehouse_id warehouse_name keywords', ''),
    'erp_catalog': ('已采集 ERP 商品目录', 'sku name goods_id able stock purchase observed_at', 'observed_at'),
    'coverage': ('最近一轮逐商品检查', 'sku name goods_id warehouse state reason', ''),
    'inventory_events': ('库存提醒历史', 'id sku name kind warehouse created delivery_status before after', 'created'),
    'messages': ('已关联商品的货源消息', 'id sku message_id text sender group created', 'created'),
    'orders': ('订单巡检匹配任务', 'id kind number shop state created delivery', 'created'),
    'shops': ('前台监控店铺', 'id name field enabled confirmed interval checked status patrol_selected', 'checked'),
    'douyin_stock': ('前台整机库存快照', 'store_id product_id product_name sku_id sku_name quantity stock_state observed erp_state erp_parts erp_shortages erp_unknown erp_checked erp_warehouse buildable', 'observed'),
    'douyin_events': ('前台库存变化历史', 'id store_id product_id product_name sku_id sku_name previous quantity kind status created', 'created'),
    'restock_records': ('补库存历史记录', 'id shop_id created detected_at inventory_at items', 'created'),
    'exclusions': ('人工排除的前台链接', 'shop_id product_id reason created', 'created'),
    'low_stock_rules': ('店铺低库存规则', 'shop_id default parts', ''),
    'price_changes': ('最近或指定批次的价格变化', 'id goods_id name before_cents after_cents delta_cents direction reason warehouse affected_count configs', ''),
    'price_affected_configs': ('价格变化影响的配置', 'id shop_id config_id product name changes cost_delta_cents complete', ''),
    'price_batches': ('价格检测批次历史', 'id started status revision', 'started'),
    'price_results': ('ERP 成本检测历史（不是调价单）', 'id run_id goods_id valid unit_cents previous_cents changed anchor_cents anchor_delta_cents reason created names configs', 'created'),
    'price_events': ('ERP 成本提醒历史', 'id shop_id created status text', 'created'),
    'price_configs': ('价格监控绑定的配置', 'shop_id config_id product name price_cents fee_cents installment parts revision', ''),
    'robots': ('通知机器人公开状态', 'id name mode enabled channels', ''),
    'notification_receipts': ('机器人逐次投递回执', 'channel job robot name status retryable updated', 'updated'),
    'monitor_sources': ('钉钉货源监听对象', 'GroupName SenderName Terms AllowReopen', ''),
    'reports': ('已生成货源周报正文', 'id name text modified_at', 'modified_at'),
    'daily_status': ('货源日报计划及最近结果', 'enabled times status reason', ''),
    'connection_status': ('库存连接状态', 'enabled connection checked_at next_check source_mode source_busy', 'checked_at'),
    'update_status': ('软件更新状态', 'phase currentVersion available version notes error received total lastResult', ''),
}


class DataError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def read_rows(path, query):
    """只读打开，不创建数据库；固定 SQL 从代码提供，外部请求不能传 SQL。"""
    path = Path(path)
    if not path.is_file():return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(query).fetchmany(LIMIT+1)]
    if len(rows)>LIMIT:raise DataError('SOURCE_LIMIT', '数据源超过十万条，本次不返回截断结果；请使用专用归档查询')
    return rows


def projected(row, fields):
    return {key:row.get(key) for key in fields.split()}


def json_rows(path, table):
    # table 仅来自下面的静态分支，不接收请求参数。
    rows = read_rows(path, 'SELECT * FROM '+table+' ORDER BY rowid')
    return [dict(json.loads(r['data']), **{k:v for k,v in r.items() if k!='data'}) for r in rows]


def catalog():
    return dict(version='inventory-data/v1', read_only=True, timezone='Asia/Shanghai',
        operators=['eq','ne','contains','in','gte','lte','is_null'], max_page_size=200,
        datasets=[dict(id=key, title=value[0], fields=value[1].split(), time_field=value[2],
            description='已保存或最近采集的数据；查询不会触发采集、匹配、发送或更新',options=['comparison: batch/day','run_id: 可选批次ID'] if key in ('price_changes','price_affected_configs','price_batches') else []) for key,value in DATASETS.items()],
        refresh=dict(dataset='price_changes',endpoint='/api/data/refresh',job_endpoint='/api/data/job',sends_notifications=False),
        unavailable=[dict(id='erp_adjustments', reason='当前 ERP 账号无调价查看权限，用户已取消接入')])


def source(console, dataset, options=None):
    b, service = console.business, console.service
    if dataset=='inventory':return service.status(include_notifications=False)['products']
    if dataset=='connection_status':return [service.status(include_notifications=False)]
    if dataset=='update_status':return [console.updater.snapshot()]
    if dataset=='products':return service.products()
    if dataset=='erp_catalog':return json_rows(service.path, 'catalog')
    if dataset=='coverage':return service.status(include_notifications=False).get('coverage', {}).get('items', [])
    if dataset=='inventory_events':
        rows = json_rows(service.path, 'events')
        fields = 'sku name goods_id warehouse_name stock able purchase monitor_qty threshold enabled'
        return [dict(r, delivery_status=r['status'], before=projected(r.get('before') or {},fields), after=projected(r.get('after') or {},fields)) for r in rows]
    if dataset=='messages':return json_rows(service.path, 'messages')
    if dataset=='orders':
        if not hasattr(console.bot,'state_root'):return console.bot.snapshot(0,100000)['jobs']
        return read_rows(console.bot.state_root/'jobs.sqlite3', 'SELECT id,kind,number,shop,state,created,delivery FROM jobs ORDER BY created,id')
    if b is None:raise DataError('SOURCE_UNAVAILABLE', '当前服务未启用业务模块')
    if dataset in ('shops','douyin_stock','douyin_events','restock_records','exclusions','low_stock_rules'):
        if b.douyin is None:raise DataError('SOURCE_UNAVAILABLE', '当前服务未启用前台库存')
        store = b.douyin.store
        if dataset=='shops':return store.stores()
        if dataset=='low_stock_rules':return [dict(shop_id=s['id'], **store.low_stock_rules(s['id'])) for s in store.stores()]
        if dataset=='exclusions':return [dict(r,shop_id=r['store'],product_id=r['product']) for r in read_rows(store.path,'SELECT store,product,reason,created FROM excluded_products ORDER BY store,product')]
        table = {'douyin_stock':'snapshots','douyin_events':'events','restock_records':'restock_records'}[dataset]
        rows = json_rows(store.path, table)
        if dataset=='restock_records':
            for r in rows:
                r['shop_id']=r['store']
                r['items']=[dict(product_id=g.get('product_id'),product_name=g.get('product_name'),**projected(item,'sku_id sku_name quantity threshold parts_checked')) for g in r.get('groups',[]) for item in g.get('skus',[])]
            return rows
        from douyin_scope import machine_rows
        rows = machine_rows(rows)
        for r in rows:
            r['store_id']=r.get('store',r.get('store_id'))
            erp=r.get('erp') or {};r['erp_state']=erp.get('state','未核对')
            r['erp_parts']=[projected(p,'goods_id name required available') for p in erp.get('parts',[])]
            # 缺口和待入记录属于 shortages，不能误从 parts 读取而丢失补货证据。
            r['erp_shortages']=[projected(p,'goods_id name required available shortage incoming') for p in erp.get('shortages',[])]
            r['erp_unknown']=erp.get('unknown',[]);r['erp_checked']=erp.get('checked');r['erp_warehouse']=erp.get('warehouse');r['buildable']=erp.get('buildable')
        return rows
    if dataset=='price_batches':return read_rows(service.root/'profit_monitor.sqlite3',"SELECT id,started,status,revision FROM runs WHERE kind='price-v2' ORDER BY started,id")
    if dataset in ('price_changes','price_affected_configs'):
        from inventory_price_changes import changes
        result=changes(service,options)
        return result[{'price_changes':'parts','price_affected_configs':'configs','price_batches':'batches'}[dataset]]
    if dataset.startswith('price_'):
        # 直接只读数据库，不调用 profit() 的惰性初始化，查询不创建业务文件。
        path = service.root/'profit_monitor.sqlite3'
        if dataset=='price_results':
            rows = read_rows(path,'SELECT * FROM price_results ORDER BY created,id')
            for r in rows:
                detail=json.loads(r['details']);r['names']=detail.get('names',[])
                r['configs']=[dict(zip(('shop_id','config_id','product','name','slot'),ref)) for ref in detail.get('refs',[]) if isinstance(ref,list) and len(ref)==5]
            return rows
        if dataset=='price_events':
            rows=read_rows(path,"SELECT id,shop_id,created,status,payload FROM events WHERE kind='price-v2' ORDER BY created,id")
            for r in rows:r['text']=json.loads(r['payload']).get('text',{}).get('content','')
            return rows
        saved=read_rows(path,'SELECT content FROM snapshot WHERE id=1')
        manifest=json.loads(saved[0]['content']) if saved else {}
        return [dict(shop_id=r['shopId'],config_id=r['configId'],product=r['product'],name=r['name'],price_cents=r['priceCents'],fee_cents=r['feeCents'],installment=r['installment'],
            parts=[projected(p,'lineId slot name goodsId qty taxCents') for p in r['parts']],revision=manifest['revision']) for r in manifest.get('configs',[])]
    if dataset in ('robots','notification_receipts'):
        # store_for_service 会做迁移；查询只选现有库的公开字段，不调用迁移入口。
        with service.db() as db:root=service.get(db,'daily_source_root',str(service.root))
        path=Path(root)/'notification_robots.sqlite3'
        if dataset=='notification_receipts':return read_rows(path,'SELECT channel,job,robot,name,status,retryable,updated FROM receipts ORDER BY updated,job,robot')
        rows=read_rows(path,"SELECT r.id,r.name,r.enabled,r.channels,CASE WHEN e.robot IS NULL THEN 'webhook' ELSE 'enterprise' END AS mode FROM robots r LEFT JOIN enterprise_robots e ON e.robot=r.id WHERE r.deleted=0 ORDER BY r.id")
        for r in rows:r['channels']=json.loads(r['channels'])
        return rows
    if dataset=='monitor_sources':
        if b.controller is None:return []
        from monitor_app_control import read_json
        return read_json(b.controller.root/'monitor_sources.json', [])
    if dataset=='reports':
        directory=service.root/'weekly_reports';rows=[]
        for p in sorted(directory.glob('*.md')) if directory.is_dir() else []:
            if p.is_symlink() or not p.resolve().is_relative_to(directory.resolve()):continue
            if p.stat().st_size>2_000_000:raise DataError('SOURCE_LIMIT','单份报告超过查询大小限制')
            rows.append(dict(id=p.name,name=p.stem,text=p.read_text(encoding='utf-8'),modified_at=p.stat().st_mtime))
        return rows
    if dataset=='daily_status':
        with service.db() as db:
            settings=dict(enabled=False,times=['11:00','16:00']);settings.update(service.get(db,'daily_settings',{}));last=service.get(db,'daily_last',{})
        return [dict(settings,status=last.get('status'),reason=last.get('reason'))]
    raise DataError('UNKNOWN_DATASET','数据集不存在')


def query(console, request):
    if not isinstance(request,dict) or set(request)-{'dataset','query','filters','page','page_size','sort','start','end','snapshot','comparison','run_id'}:
        raise DataError('INVALID_QUERY','查询包含不支持的参数')
    dataset=request.get('dataset')
    if not isinstance(dataset,str) or dataset not in DATASETS:raise DataError('UNKNOWN_DATASET','数据集不存在；请先读取查询目录')
    if (request.get('comparison') or request.get('run_id')) and dataset not in ('price_changes','price_affected_configs','price_batches'):raise DataError('INVALID_QUERY','该数据集不支持价格比较参数')
    title, fields, time_field=DATASETS[dataset];allowed=set(fields.split())
    filters=request.get('filters',[]);needle=request.get('query','')
    if not isinstance(filters,list) or len(filters)>20 or not isinstance(needle,str) or len(needle)>300:raise DataError('INVALID_QUERY','筛选格式无效')
    for f in filters:
        if not isinstance(f,dict) or set(f)-{'field','op','value'} or not isinstance(f.get('field'),str) or f.get('field') not in allowed or f.get('op') not in catalog()['operators']:raise DataError('INVALID_FILTER','筛选字段或操作不支持')
        if f['op']=='is_null' and type(f.get('value')) is not bool:raise DataError('INVALID_FILTER','is_null 条件需要布尔值')
        if f['op']=='in' and (not isinstance(f.get('value'),list) or len(f['value'])>100):raise DataError('INVALID_FILTER','in 条件需要最多100项的列表')
    page,size=request.get('page',0),request.get('page_size',50)
    if type(page) is not int or page<0 or type(size) is not int or not 1<=size<=200:raise DataError('INVALID_PAGE','页码从0开始，每页1至200条')
    bounds=[]
    for key in ('start','end'):
        if request.get(key):
            if not time_field:raise DataError('INVALID_TIME','该数据集没有时间字段')
            try:
                stamp=datetime.fromisoformat(request[key]);stamp=stamp.replace(tzinfo=TZ) if stamp.tzinfo is None else stamp
                if key=='end' and len(request[key])==10:stamp+=timedelta(days=1)
                bounds.append((key,stamp.timestamp()))
            except (ValueError,TypeError):raise DataError('INVALID_TIME','时间须为 ISO 日期或时间，默认北京时间') from None
    if len(bounds)==2 and bounds[0][1]>bounds[1][1]:raise DataError('INVALID_TIME','开始时间晚于结束时间')
    try:rows=[projected(r,fields) for r in source(console,dataset,request)]
    except DataError:raise
    except Exception:raise DataError('SOURCE_UNAVAILABLE','数据源暂不可读，请核对模块状态后重试') from None
    if len(rows)>LIMIT:raise DataError('SOURCE_LIMIT','结果超过十万条，未返回截断数据')
    revision=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    if request.get('snapshot') and request['snapshot']!=revision:raise DataError('SNAPSHOT_CHANGED','数据已变化，请从第一页重新查询')
    def match(row):
        if needle.casefold() not in json.dumps(row,ensure_ascii=False,default=str).casefold():return False
        for key,bound in bounds:
            value=row.get(time_field)
            if isinstance(value,str):
                try:
                    stamp=datetime.fromisoformat(value);value=(stamp.replace(tzinfo=TZ) if stamp.tzinfo is None else stamp).timestamp()
                except ValueError:return False
            if not isinstance(value,(float,int)) or (value<bound if key=='start' else value>=bound):return False
        for f in filters:
            value=row.get(f['field']);expected=f.get('value');op=f['op']
            if op=='eq' and value!=expected:return False
            if op=='ne' and value==expected:return False
            if op=='in' and value not in expected:return False
            if op=='is_null' and (value is None)!=bool(expected):return False
            if op=='contains' and str(expected).casefold() not in str(value or '').casefold():return False
            if op in ('gte','lte'):
                try:
                    if value is None or (value<expected if op=='gte' else value>expected):return False
                except TypeError:raise DataError('INVALID_FILTER','比较值与字段类型不匹配') from None
        return True
    rows=[r for r in rows if match(r)]
    order=request.get('sort')
    if order:
        if not isinstance(order,dict) or set(order)-{'field','direction'} or not isinstance(order.get('field'),str) or order.get('field') not in allowed or order.get('direction','asc') not in ('asc','desc'):raise DataError('INVALID_SORT','排序字段或方向无效')
        def sort_key(row):
            value=row.get(order['field'])
            return (value is None,0 if isinstance(value,(int,float)) else 1,value if isinstance(value,(int,float)) else str(value))
        rows.sort(key=sort_key,reverse=order.get('direction')=='desc')
    total=len(rows);offset=page*size
    return dict(version='inventory-data/v1',dataset=dataset,title=title,read_only=True,source='local_saved_data',
        snapshot=revision,total=total,page=page,page_size=size,has_more=offset+size<total,rows=rows[offset:offset+size],
        time_field=time_field,timezone='Asia/Shanghai',live_query=False)
