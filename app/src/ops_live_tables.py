"""只读复用部门现有钉钉明细表，按稳定键核对授权范围。"""
from concurrent.futures import ThreadPoolExecutor
import json
import re

from ops_command_api import denied
from ops_sources import COUNTS, ReportTables, plain, safe_link, stamp, unavailable

FIELDS = {
    'shipping_orders': {'key','checked','urgency'},
    'shipping_shortages': {'key','stores','checked','status','goods_id','name','gap','physical_gap','incoming'},
    'front_stock': {'key','spu','sku_id','product_name','config_name','platform_stock','parts_state',
                    'replenish_state','handling_state','snapshot_at','parts_checked_at'},
}
ACTIVE_FLAGS = ('待补库存','低库存','待人工核对')


def cell(value):
    value = plain(value)
    if isinstance(value, dict):
        return value.get('name')
    return value


def amount(value):
    # DWS 数字字段可能回读为纯数字字符串；未知和 0 保持区别。
    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,15}(?:\.0+)?',value.strip()):
        value = int(value.strip().split('.')[0])
    if type(value) is float and value.is_integer():
        value = int(value)
    return value if type(value) is int and value>=0 else None


def binding_scope(item, name, org):
    fields = item['fields']
    if set(fields)!=FIELDS[name] or len(set(fields.values()))!=len(fields) or not all(fields.values()):
        raise ValueError('在线明细表字段映射不完整或重复')
    if item['profile'].partition(':')[0]!=org or not item['base_id'] or not item['table_id']:
        raise ValueError('在线明细表不属于机器人组织')
    scope = item['shops']
    if not scope or any(not key.isdecimal() or not value for key,value in scope.items()):
        raise ValueError('在线明细表店铺范围无效')
    if name=='front_stock' and (len(set(scope.values()))!=len(scope)
                                or any(not re.fullmatch('[0-9a-f]{32}',v) for v in scope.values())):
        raise ValueError('前台库存店铺标识重复或无效')
    if name=='shipping_shortages' and len(set(scope.values()))!=len(scope):
        raise ValueError('缺货表店铺名称重复')


def filters(*conditions):
    return dict(operator='and',operands=list(conditions))


def compare(op, field, value):
    return dict(operator=op,operands=[field,value])


class LiveReportTables(ReportTables):
    def __init__(self, batch, detail, org, *, query=None):
        super().__init__(batch)
        self.detail, self.query = detail, query or self._query
        for name, binding in detail.items():
            binding_scope(binding,name,org)
        # 能力发现与批次表保持相同契约；有既有明细时优先查询明细。
        self.bindings = {**batch, **detail}

    @staticmethod
    def _query(binding, query=None):
        from douyin_online import DwsTable
        command = ['record','query','--all','--page-limit','30','--field-ids',','.join(binding['fields'].values())]
        if query:
            command += ['--filters',json.dumps(query,ensure_ascii=False)]
        data = DwsTable(binding).call(command)
        if data.get('complete') is not True or data.get('hasMore') or not isinstance(data.get('records'),list):
            raise ValueError('在线明细未完整读取')
        return data['records']

    def _rows(self, config, query=None):
        seen = set()
        for entry in self.query(config,query):
            ident = entry['recordId']
            if not isinstance(ident,str) or not ident or ident in seen:
                raise ValueError('在线明细出现重复或无效记录')
            seen.add(ident)
            yield {key:cell(entry['cells'].get(field)) for key,field in config['fields'].items()}

    def _shipping_table(self, name, shops):
        config = self.detail.get(name)
        if not config:
            return dict(source=name,state='not_bound',counts={})
        if not shops <= config['shops'].keys():
            return dict(source=name,state='forbidden',counts={})
        is_order = name=='shipping_orders'
        key_names = COUNTS[:4] if is_order else COUNTS[4:]
        count = {k:None for k in key_names}
        scoped, bad = [], False
        seen = set()
        aliases = {label:key for key,label in config['shops'].items()}
        try:
            for row in self._rows(config):
                if is_order:
                    key = row.get('key')
                    match = re.fullmatch(r'([0-9]+):NO-[A-Za-z0-9-]+',key) if isinstance(key,str) else None
                    if not match or match[1] not in config['shops']:
                        bad = True
                        continue
                    if match[1] not in shops:
                        continue
                else:
                    if row.get('status')=='本轮无待发需求':
                        continue
                    names = set(re.split(r'[、，,]+',row.get('stores') or ''))
                    names = {n.strip() for n in names if n.strip()}
                    if not names or not names <= aliases.keys():
                        bad = True
                        continue
                    row_scope = {aliases[n] for n in names}
                    if not row_scope <= shops:
                        # 全店联合缺口不拆分给单店，也不返回其它店铺的汇总。
                        bad |= bool(row_scope & shops)
                        continue
                    key = row.get('key')
                at = stamp(row.get('checked'))
                if not isinstance(key,str) or not key or key in seen or at is None:
                    bad = True
                    continue
                seen.add(key)
                scoped.append((at,row))
        except Exception:
            return dict(source=name,state='read_failed',counts=count)
        if not scoped:
            return dict(source=name,state='partial' if bad else 'no_report',counts=count)
        latest = max(at for at,_ in scoped)
        rows = [row for at,row in scoped if at==latest]
        bad |= len(rows)!=len(scoped)
        items = []
        if is_order:
            # 原表只登记超时、紧急等待揽收关注项，不能冒充待发总数。
            count.update(overdue=sum(r['urgency']=='已超时' for r in rows),
                         due_within_48h=sum(r['urgency'] in ('紧急','48 小时内到期') for r in rows))
            bad = True
        else:
            items = [dict(goods_id=r['goods_id'],name=str(r.get('name') or '')[:200],
                          shortage=max(amount(r.get('gap')) or 0,amount(r.get('physical_gap')) or 0),
                          incoming=amount(r.get('incoming'))) for r in rows
                     if r['status']=='缺货' and isinstance(r['goods_id'],str) and r['goods_id'].isascii()
                     and r['goods_id'].isdecimal()]
            items = sorted((r for r in items if r['shortage']>0),key=lambda r:-r['shortage'])[:5]
            count.update(shortage_goods=sum(r['status']=='缺货' for r in rows),
                         inventory_unknown_goods=sum(r['status']=='库存待核实' for r in rows))
        return dict(source=name,state='partial' if bad else 'verified',checked_at=latest,batch_id=f'detail:{int(latest)}',
                    counts=count,shortage_items=items,table_url=safe_link(config.get('url'))
                    if shops==config['shops'].keys() and not config.get('hide_link') else None)

    def shipping(self, shops):
        if not {'shipping_orders','shipping_shortages'} & self.detail.keys():
            return super().shipping(shops)
        with ThreadPoolExecutor(max_workers=2) as pool:
            names = ('shipping_orders','shipping_shortages')
            reports = list(pool.map(lambda name:self._shipping_table(name,shops),names))
        if all(r['state']=='forbidden' for r in reports):
            raise denied()
        if all(r['state'] in ('read_failed','not_bound','forbidden') for r in reports):
            raise unavailable('本次无法读取发货监控 AI 表格')
        found = any('checked_at' in r for r in reports)
        return dict(outcome='partial' if found or any(r['state']=='partial' for r in reports) else 'no_report',
                    coverage='partial',shop_ids=sorted(shops),tables=reports,counts=dict.fromkeys(COUNTS),
                    source_note='来自现有待揽收关注表和缺货表，以下是表中最近一次记录，并非实时待发总量。')

    def front(self, shop, args):
        config = self.detail.get('front_stock')
        if not config:
            return super().front(shop,args)
        store = config['shops'].get(shop)
        if not store:
            raise denied()
        f = config['fields']
        # 先按店铺键及活跃标记限制远端记录，再用完整三段键二次核对。
        conditions = [compare('contain',f['key'],store+':'),
                      compare('ne',f['replenish_state'],'已退出提醒')]
        conditions.extend(compare('eq',f[k],args[k]) for k in ('spu','sku_id') if args.get(k))
        items, conflicts, partial = {}, set(), False
        try:
            for row in self._rows(config,filters(*conditions)):
                key = row.pop('key')
                spu,sku = row['spu'],row['sku_id']
                if not isinstance(key,str) or not key.startswith(store+':'):
                    continue
                if not all(isinstance(v,str) and re.fullmatch(r'[0-9]+',v) for v in (spu,sku)) or key!=f'{store}:{spu}:{sku}':
                    partial = True
                    continue
                if row['replenish_state'] not in ACTIVE_FLAGS or any(row[k]!=args[k] for k in ('spu','sku_id') if args.get(k)):
                    continue
                row['platform_stock'] = amount(row['platform_stock'])
                if key in items:
                    conflicts.add(key)
                else:
                    items[key] = row
        except Exception:
            raise unavailable('本次无法完整读取前台库存 AI 表格') from None
        for key in conflicts:
            del items[key]
        latest = max((stamp(r['snapshot_at']) or 0 for r in items.values()),default=0)
        rows = sorted(items.values(),key=lambda r:(-(stamp(r['snapshot_at']) or 0),r['spu'],r['sku_id']))
        partial = partial or bool(conflicts)
        return dict(shop_id=shop,outcome='partial' if partial else 'ready' if rows else 'no_report',
                    checked_at=latest or None,coverage='partial' if partial else 'complete',detail_source=True,
                    counts=dict(total=len(rows),shown=min(5,len(rows))),items=rows[:5],
                    **{k:args[k] for k in ('spu','sku_id') if k in args})
