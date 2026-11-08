"""价格变化视图：复用成本检测记录，按批次或北京自然日比较并关联配置。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from contextlib import closing

TZ=timezone(timedelta(hours=8))


def movement(before, after):
    # 与配件工作台 priceMovement 一致：从零上涨归入大幅上涨，百分比保持未知。
    if after is None:return dict(band='missing',percent=None)
    if before is None:return dict(band='first',percent=None)
    delta=after-before;percent=delta/before*100 if before>0 else None
    band=('high' if percent is None or delta*100>=before*5 else 'up') if delta>0 else 'down' if delta<0 else 'same'
    return dict(band=band,percent=percent)



def changes(service, options=None):
    options=options or {};path=Path(service.root)/'profit_monitor.sqlite3'
    empty=dict(parts=[],configs=[],batches=[],run_id=None,checked_at=None,comparison=options.get('comparison','batch'),message='尚无成本检测记录；先绑定配置快照，再查看价格变动。')
    if not path.is_file():return empty
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        batches=[dict(r) for r in db.execute("SELECT id,started,status,revision FROM runs WHERE kind='price-v2' ORDER BY started DESC,id DESC LIMIT 100")]
        ident=options.get('run_id') or (batches[0]['id'] if batches else None)
        if not ident:return empty
        run=db.execute("SELECT id,started,status,revision FROM runs WHERE id=? AND kind='price-v2'",(ident,)).fetchone()
        if run is None:raise ValueError('价格检测批次不存在')
        rows=[dict(r) for r in db.execute('SELECT * FROM price_results WHERE run_id=? ORDER BY goods_id,id',(ident,)).fetchmany(100001)]
        if len(rows)>100000:raise ValueError('价格检测结果超过读取上限，未返回截断结果')
        day=datetime.fromtimestamp(run['started'],TZ).replace(hour=0,minute=0,second=0,microsecond=0)
        previous={}
        if options.get('comparison')=='day':
            # 昨日必须有有效采集；不拿更早价格冒充昨日价格。
            for row in db.execute('SELECT * FROM price_results WHERE valid=1 AND created>=? AND created<? ORDER BY created,id',((day-timedelta(days=1)).timestamp(),day.timestamp())):
                previous[(row['source_id'],row['workspace_id'],row['sql_scope'],row['goods_id'])]=dict(row)
    if options.get('comparison','batch') not in ('batch','day'):raise ValueError('请选择逐批次或当日对比')
    parts=[];config_map={};needle=str(options.get('query','')).casefold();direction=options.get('direction','all')
    if direction not in ('all','up','down','same','baseline','unknown','high','small_up'):raise ValueError('价格变化筛选无效')
    for row in rows:
        detail=json.loads(row['details']);refs=detail.get('refs',[])
        before=row['previous_cents']
        if options.get('comparison')=='day':
            old=previous.get((row['source_id'],row['workspace_id'],row['sql_scope'],row['goods_id']))
            before=old['unit_cents'] if old else None
        after=row['unit_cents'] if row['valid'] else None
        delta=after-before if after is not None and before is not None else None
        kind='unknown' if after is None else 'baseline' if before is None else 'up' if delta>0 else 'down' if delta<0 else 'same'
        name='、'.join(detail.get('names',[])) or row['goods_id']
        classification=movement(before,after)
        selected=classification['band']=='high' if direction=='high' else classification['band']=='up' if direction=='small_up' else kind==direction
        if direction!='all' and not selected:continue
        if needle and needle not in json.dumps([name,row['goods_id'],refs],ensure_ascii=False).casefold():continue
        quantity={(str(s),str(c)):q for s,c,q in detail.get('configQuantities',[])}
        unique={}
        for shop,config,product,title,slot in refs:
            key=(shop,config)
            unique[key]=dict(shop_id=shop,config_id=config,product=product,name=title)
        part=dict(id=row['id'],goods_id=row['goods_id'],name=name,before_cents=before,after_cents=after,delta_cents=delta,
            direction=kind,**classification,reason=row['reason'],warehouse='公司大库',affected_count=len(unique),configs=list(unique.values()))
        parts.append(part)
        if kind not in ('up','down','unknown','baseline'):continue
        for key,ref in unique.items():
            config=config_map.setdefault(key,dict(ref,id=json.dumps(key,ensure_ascii=False),changes=[],cost_delta_cents=0,complete=True))
            qty=quantity.get(key)
            amount=delta*qty if delta is not None and type(qty) is int and qty>0 else None
            config['changes'].append(dict(goods_id=row['goods_id'],name=name,quantity=qty,before_cents=before,after_cents=after,delta_cents=delta,cost_delta_cents=amount))
            if amount is None:config['complete']=False
            else:config['cost_delta_cents']+=amount
    configs=list(config_map.values())
    for item in configs:
        if not item['complete']:item['cost_delta_cents']=None
    return dict(parts=parts,configs=configs,batches=batches,run_id=ident,checked_at=run['started'],run_status=run['status'],revision=run['revision'],
        comparison=options.get('comparison','batch'),message='公司大库 ERP 成本检测对比；未改售价。首次采集、昨日缺失或历史配件数量缺失时保留未知。',
        changed_count=sum(r['direction'] in ('up','down') for r in parts),unknown_count=sum(r['direction']=='unknown' for r in parts))


def result_tables(snapshot):
    from inventory_business_views import table as T
    money=lambda value:'—' if value is None else f'{value/100:+.2f}'
    price=lambda value:'—' if value is None else f'{value/100:.2f}'
    labels=dict(up='涨价',down='降价',same='未变化',baseline='建立基准',unknown='待核对')
    parts=[dict(r,before=price(r['before_cents']),after=price(r['after_cents']),delta=money(r['delta_cents']),state=dict(high='上涨 ≥5% / 从零上涨',up='上涨 <5%',down='降价',same='未变化',first='首次采集',missing='价格缺失')[r['band']],percent_text='—' if r['percent'] is None else f"{r['percent']:+.2f}%",
        _details=[dict(label=c['product']+' / '+c['name'],value='店铺 '+c['shop_id']+' · 配置 '+c['config_id']) for c in r['configs']]) for r in snapshot['parts']]
    configs=[dict(r,delta=money(r['cost_delta_cents']),count=len(r['changes']),
        _details=[dict(label=c['name']+' · '+c['goods_id'],value=price(c['before_cents'])+' → '+price(c['after_cents'])+'；每台数量 '+str(c['quantity'] or '未知')+'；成本影响 '+money(c['cost_delta_cents'])) for c in r['changes']]) for r in snapshot['configs']]
    return [T('配件价格变化',[('name','配件'),('goods_id','ERP 编号'),('before','调整前成本'),('after','调整后成本'),('delta','涨跌金额'),('percent_text','涨跌幅'),('state','变化划分'),('affected_count','关联配置')],parts),
        T('受影响的配置',[('shop_id','店铺'),('product','链接'),('name','配置'),('count','变动配件数'),('delta','筛选内每台成本变化')],configs)]
