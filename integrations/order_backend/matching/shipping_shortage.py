"""按配件汇总缺货与待入；待客审使用可销口径，已占用需求不重复扣减。"""
from __future__ import annotations

from pathlib import Path
from shipping_monitor import number


def optional_number(value):
    # 空值保持未知，不能把缺失的待入或库存变成零。
    try:
        return number(value)
    except (ValueError, TypeError):
        return None


def summarize(orders, stocks, complete):
    goods = {}
    for order in orders:
        for part in order['parts']:
            gid = part['goodsId']
            item = goods.setdefault(gid, {'goodsId':gid, 'code':part.get('code',''), 'name':part['name'],
                'review':0, 'printing':0, 'printed':0, 'orders':set(), 'shops':set()})
            state_field = {'0':'review','6':'printing','9':'printed'}[order['state']]
            item[state_field] += part['quantity']
            item['orders'].add(order['shop']+' '+order['code'])
            item['shops'].add(order['shop'])
    result = []
    for gid,item in goods.items():
        stock = stocks.get(gid,{})
        available, physical = optional_number(stock.get('available')), optional_number(stock.get('stock'))
        incoming = optional_number(stock.get('allIncoming'))
        item['code'] = stock.get('stockCode') or item['code']
        total = item['review'] + item['printing'] + item['printed']
        # 缺口不超过本批需求；负可销仍原样展示，但按零可用参与本批计算。
        gap = max(0,item['review']-max(0,available)) if available is not None else None
        physical_gap = max(0,total-max(0,physical)) if physical is not None else None
        status = '缺货' if (gap or 0)>0 or (physical_gap or 0)>0 else (
            '库存待核实' if gap is None or physical_gap is None else
            '本轮未见缺口' if complete else '需求覆盖不完整，待核实')
        item.update(available=available, stock=physical, allIncoming=incoming, total=total, gap=gap,
                    physicalGap=physical_gap, status=status, incomingStatus='待入未提供' if incoming is None else '有待入' if incoming>0 else '无待入',
                    orders=sorted(item['orders']), shops=sorted(item['shops']))
        result.append(item)
    return sorted(result,key=lambda x:(-(x['gap'] or 0),-(x['physicalGap'] or 0),x['goodsId']))


def cells_for(item, report, fields):
    values = {'配件键':item['goodsId'],'商品编码':item['code'],'商品名称':item['name'],
        '待客审本批需求':item['review'],'公司大库可销':item['available'],'本批缺口':item['gap'],
        '全库待入':item['allIncoming'],'待入情况':item['incomingStatus'],'待打印需求':item['printing'],
        '已打印需求':item['printed'],'待发总需求':item['total'],'公司大库现货':item['stock'],
        '待发总需求现货缺口':item['physicalGap'],'涉及订单数':len(item['orders']),
        '涉及店铺':'、'.join(item['shops']),'涉及订单':'\n'.join(item['orders']),
        '缺货状态':item['status'],'巡检时间':report['checkedAt'][:19].replace('T',' '),
        '统计说明':'三店合计；本批缺口仅待客审需求对比可销；两类缺口不能相加。'+
                    ('' if report['complete'] else f"本轮有{len(report['errors'])}项查询异常，需求为已读取部分。")}
    return {fields[k]:v for k,v in values.items()}


def sync_shortages(report, root, table_factory=None):
    from shipping_online import Table, read_json, save_json
    from patrol_service import single_instance
    root = Path(root)
    config = read_json(root/'shipping-online.json',{})
    target = config.get('shortageTable')
    if not config.get('enabled') or not target or 'stockSummary' not in report:
        return {'enabled':False}
    result = {'enabled':True,'complete':False,'updated':0,'errors':[],'shortageCount':sum(x['status']=='缺货' for x in report['stockSummary'])}
    # 同步结果携带当前绑定的明细链接，通知可直接展示已核验的在线表。
    result['url'] = target.get('url', '')
    with single_instance(root/'shipping-shortage.lock') as acquired:
        if not acquired:
            return dict(result,errors=['缺货表正在同步'])
        cfg = dict(config, tableId=target['tableId'], fields=target['fields'])
        table = (table_factory or Table)(cfg,root)
        path = root/'shipping-shortage-state.json'
        state = read_json(path,{'managed':{},'pending':{}})
        try:
            remote = table.records()
            by_key = {}
            key_field = cfg['fields']['配件键']
            for row in remote:
                key = row.get('cells',{}).get(key_field)
                if not key:
                    continue
                if key in by_key:
                    raise ValueError('缺货表配件键重复，停止同步')
                by_key[key] = row
            desired = {x['goodsId']:cells_for(x,report,cfg['fields']) for x in report['stockSummary']
                       if x['status'] in ('缺货','库存待核实') or x['goodsId'] in state['managed'] or x['goodsId'] in state['pending']}
            # 已恢复配件保留一行并更新状态，不让上轮缺口永久滞留，也不擅自删行。
            for key in state['managed']:
                if key in desired or key not in by_key:
                    continue
                f=cfg['fields']
                if report['complete']:
                    values={f[n]:0 for n in ('待客审本批需求','本批缺口','待打印需求','已打印需求','待发总需求','待发总需求现货缺口','涉及订单数')}
                    values.update({f['缺货状态']:'本轮无待发需求',f['涉及订单']:'',f['涉及店铺']:'',f['统计说明']:'本轮需求为零；库存与待入数值保留上次快照。'})
                else:
                    values={f['缺货状态']:'本轮待核实',f['统计说明']:'查询不完整，保留上次需求与库存数值，不能认定已恢复。'}
                values.update({key_field:key,f['巡检时间']:report['checkedAt'][:19].replace('T',' ')})
                desired[key]=values
            for key,cells in desired.items():
                try:
                    row = by_key.get(key)
                    owned = state['managed'].get(key)
                    if owned and any(r['recordId']==owned and r.get('cells',{}).get(key_field)!=key for r in remote):
                        raise ValueError('配件键被人工修改，未覆盖')
                    if key in state['pending'] and not row:
                        raise ValueError('上次写入未知，未重复创建')
                    state['pending'][key]=True
                    save_json(path,state)
                    rid=table.write(row['recordId'] if row else None,cells)
                    state['managed'][key]=rid;state['pending'].pop(key,None)
                    save_json(path,state);result['updated']+=1
                except Exception as error:
                    from group_order_bot import safe_error
                    result['errors'].append(key+'：'+safe_error(error))
            result['complete']=not result['errors']
        except Exception as error:
            from group_order_bot import safe_error
            result['errors'].append(safe_error(error))
        save_json(root/'shipping-shortage-last-sync.json',result)
    return result
