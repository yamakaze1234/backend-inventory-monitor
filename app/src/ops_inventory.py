"""库存与价格指令：只读来源分开投影，关注写入复用现有库存事务。"""
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
import sqlite3
import time
import unicodedata

from inventory_rules import validate_product
from ops_command_api import CommandError, bad, denied


def normalized(text):
    # 保留 Ti、SUPER、容量等修饰；只统一大小写、全半角与型号空格。
    text = unicodedata.normalize('NFKC', text).casefold()
    text = re.sub(r'\b(rtx|gtx|rx)(?=\d{4})', r'\1 ', text)
    text = re.sub(r'\b(\d{4})\s*(ti|super)', r'\1\2', text)
    text = re.sub(r'\b(\d+)\s*(?:gb|g)(?![a-z])', r'\1g', text)
    return re.sub(r'\s+', ' ', text).strip()


def condition(text, term):
    # 数字型号与容量不能用子串匹配，例如 8G 不应命中 48G。
    if re.fullmatch(r'[a-z0-9.-]+', term):
        return bool(re.search(r'(?<![a-z0-9])' + re.escape(term) + (r'(?!\d)' if re.fullmatch(r'\d{4}', term) else r'(?![a-z0-9])'), text))
    return term in text


def rows_for(db):
    return [json.loads(raw) for (raw,) in db.execute('SELECT data FROM catalog ORDER BY sku')]


def ordered(rows):
    unique = {}
    for row in sorted(rows, key=lambda r: (normalized(r['name']), str(r.get('goods_id', '')), r['sku'])):
        gid = str(row.get('goods_id', ''))
        if re.fullmatch(r'[1-9][0-9]{0,19}', gid):
            unique.setdefault(gid, row)
    return list(unique.values())


def keyword_rows(rows, phrase, extra=()):
    exact = [r for r in rows if phrase in (normalized(r['sku']), normalized(r['name']))]
    if exact and not extra:
        return exact
    terms = phrase.split() + list(extra)
    return [r for r in rows if all(condition(normalized(r['name'] + ' ' + r['sku']), term) for term in terms)]


def search(rows, args, interpret=None, aliases=None):
    if 'goods_id' in args:
        return ordered(r for r in rows if str(r.get('goods_id')) == args['goods_id']), 'exact_id', [], []
    raw = args['keyword']
    phrase = normalized(raw)
    # 只应用本地已核对的整词别名，避免简称替换时吞掉显存或型号后缀。
    aliases = aliases or {}
    for alias, term in sorted(aliases.items(), key=lambda row: -len(row[0])):
        phrase = re.sub(r'(?<!\S)' + re.escape(alias) + r'(?!\S)', lambda _: term, phrase)
    found = ordered(keyword_rows(rows, phrase))
    if found or not interpret or not rows:
        return found, 'keyword', [], []
    # AI 只提供检索词；原话可识别的型号、后缀、显存和颜色始终作为硬条件。
    required = tuple(dict.fromkeys(re.findall(r'\d{4}(?:ti|super)?|\d+g|(?<![a-z])(?:ti|super)(?![a-z])|白色|黑色|银色|红色|蓝色', phrase)))
    try:
        result = interpret(raw)
        if not isinstance(result, dict) or set(result) != {'candidates', 'conditions'}:
            raise ValueError
        candidates, terms = result['candidates'], result['conditions']
        if not (isinstance(candidates, list) and 1 <= len(candidates) <= 3
                and isinstance(terms, list) and len(terms) <= 10
                and all(isinstance(x, str) and 0 < len(x.strip()) <= 80 and x.isprintable() for x in candidates + terms)):
            raise ValueError
        normalized_conditions = tuple(dict.fromkeys((*required, *(normalized(x) for x in terms))))
        matches = [ordered(keyword_rows(rows, normalized(x), normalized_conditions)) for x in candidates]
        verified = ordered(r for group in matches for r in group)
        alternatives = ordered(r for x in candidates for r in keyword_rows(rows, normalized(x)))
        models = {m for r in verified for m in re.findall(r'\d{4}(?:ti|super)?', normalized(r['name']))}
        if verified and (len(models) <= 1 or any(re.fullmatch(r'\d{4}(?:ti|super)', t) for t in normalized_conditions)):
            return verified, 'ai_assisted', list(normalized_conditions), []
        return [], 'keyword', [], verified or alternatives
    except Exception:
        return [], 'keyword', [], []


def snapshot(service, db, row, warehouse_id):
    sku, gid = row['sku'], str(row['goods_id'])
    saved = db.execute('SELECT data FROM snapshots WHERE sku=?', (sku,)).fetchone()
    observations = [row, json.loads(saved[0])] if saved else [row]
    observations = [r for r in observations if str(r.get('goods_id')) == gid]
    if warehouse_id:
        options = service.get(db, 'warehouse_options:' + gid, {})
        matching = [dict(qty=r.get('warehouse_stock'), warehouse_name=r.get('warehouse_name'), observed_at=r.get('observed_at'))
                    for r in observations if r.get('warehouse_id') == warehouse_id and r.get('stock_basis') == 'warehouse_stock']
        if options.get('status') == 'ready' and options.get('scope') == service.get(db, 'scope'):
            matching += [dict(qty=d.get('stock'), warehouse_name=d['name'], observed_at=options['observed_at'])
                         for d in options['depots'] if d['id'] == warehouse_id]
        latest = max(matching, default={}, key=lambda r: r.get('observed_at') or 0)
        return dict(latest, warehouse_id=warehouse_id, stock_basis='warehouse_stock',
                    warehouse_name=latest.get('warehouse_name') or warehouse_id, qty=latest.get('qty'))
    latest = max(observations, default={}, key=lambda r: r.get('observed_at') or 0)
    at = latest.get('observed_at') or None
    # 完整目录有时只有库存目录值；没有采集时间或库存未知时不当作实际可销。
    return dict(qty=latest.get('able') if at and not latest.get('stock_unknown') else None,
                warehouse_name='公司大库', stock_basis='company_able', observed_at=at,
                erp_able=latest.get('erp_able') if at else None, pending_review=latest.get('pending_review') if at else None)


def cents(value):
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 100_000_000:
            return None
        return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def maintenance_prices(bindings, ids, shops):
    """用已核对的列名读已保存数值；逐店核对 SPU、SKU 和配件关系。"""
    from douyin_maintenance import digest, headers, read_maintenance, workbook
    scoped = [b for b in bindings if b['shop_id'] in shops]
    results = {gid: [] for gid in ids}
    states = {gid: ('not_bound' if not scoped else 'no_match') for gid in ids}
    issues = {gid: set() for gid in ids}
    for binding in scoped:
        path, sheet, shop = (binding[k] for k in ('path', 'sheet', 'shop_id'))
        try:
            saved = read_maintenance(path, sheet, shop)
            by_row = {row['row']: (key, row) for key, row in saved['rows'].items() if row['state'] == 'ready' and ids.intersection(row['parts'])}
            with workbook(path) as (_, rows):
                _, columns = headers(rows, sheet)
                part_columns = binding.get('part_price_columns', {})
                host_column = columns.get(binding.get('host_price_column'))
                relevant = {gid for _, row in by_row.values() for gid in ids.intersection(row['parts'])}
                prices = {gid: [] for gid in ids}
                for number, values in rows(sheet):
                    if number not in by_row:
                        continue
                    key, row = by_row[number]
                    spu, sku = key.split(':', 1)
                    for gid in ids.intersection(row['parts']):
                        unit = set()
                        for slot, column in part_columns.items():
                            if str(values.get(columns.get(slot + '_id'), '')).removesuffix('.0') == gid:
                                value = cents(values.get(columns.get(column)))
                                if value is not None:
                                    unit.add(value)
                        if len(unit) > 1:
                            issues[gid].add('conflict')
                            continue
                        amount = next(iter(unit)) if unit else cents(values.get(host_column))
                        if amount is None:
                            issues[gid].add('column_unverified')
                            continue
                        prices[gid].append(dict(shop_id=shop, spu=spu, sku_id=sku, config_name=row['name'],
                                                price_kind='part_unit_price' if unit else 'host_checkout_price', price_cents=amount))
            if digest(path) != saved['sha256']:
                raise ValueError('读取期间表格发生变化')
            for gid in relevant:
                results[gid].extend(prices[gid])
        except Exception:
            for gid in ids:
                issues[gid].add('read_failed')
    for gid in ids:
        dedup = {}
        for price in results[gid]:
            key = tuple(price[k] for k in ('shop_id', 'spu', 'sku_id', 'price_kind'))
            if key in dedup and dedup[key] != price:
                issues[gid].add('conflict')
            dedup[key] = price
        results[gid] = [] if 'conflict' in issues[gid] else [dedup[k] for k in sorted(dedup)]
        for state in ('conflict', 'read_failed', 'column_unverified'):
            if state in issues[gid]:
                states[gid] = state
                break
        else:
            if results[gid]:
                states[gid] = 'ready'
    return results, states


class InventoryCommands:
    def __init__(self, service, *, cost=None, ai=None, maintenance=(), aliases=None):
        self.service, self.cost, self.ai, self.maintenance = service, cost, ai, maintenance
        self.aliases = {normalized(k): normalized(v) for k, v in (aliases or {}).items()
                        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}

    def inventory(self, args, access):
        shops = access.shops
        if args.get('shop_id'):
            if args['shop_id'] not in shops:
                raise denied()
            shops = {args['shop_id']}
        with self.service.db() as db:
            rows = rows_for(db)
        found, mode, terms, candidates = search(rows, args, self.ai, self.aliases)
        if candidates:
            return dict(outcome='needs_clarification', candidates=[dict(name=r['name'], goods_id=str(r['goods_id'])) for r in candidates[:5]])
        if not found:
            raise CommandError(404, 'NOT_FOUND', '未找到匹配商品')
        items = []
        with self.service.db() as db:
            for row in found[:5]:
                obs = snapshot(self.service, db, row, args.get('warehouse_id'))
                at = obs.get('observed_at')
                obs['stock_status'] = ('unknown' if obs.get('qty') is None else 'fresh' if at and
                                       0 <= time.time() - at <= self.service.get(db, 'interval_seconds', 7200) + 300 else 'historical')
                items.append(dict(name=row['name'], sku=row['sku'], goods_id=str(row['goods_id']), **obs))
        ids = {r['goods_id'] for r in items}
        if 'tax_cost' in access.fields:
            from ops_tax_cost import quote_costs
            tax_costs = quote_costs(self.service, ids)
            for item in items:
                item.update(tax_costs[item['goods_id']])
        cost, cost_at, cost_state = {}, None, 'forbidden'
        if 'erp_cost' in access.fields:
            try:
                if not self.cost:
                    raise ValueError
                result = self.cost(ids)
                cost, cost_at, cost_state = result['costs'], result['collectedAt'], 'unknown'
            except Exception:
                cost_state = 'read_failed'
        prices, states = ({gid: [] for gid in ids}, {gid: 'forbidden' for gid in ids})
        if 'maintenance_price' in access.fields:
            prices, states = maintenance_prices(self.maintenance, ids, shops)
        for item in items:
            gid = item['goods_id']
            amount = cost.get(gid)
            amount = amount if type(amount) is int and amount >= 0 else None
            item.update(erp_unit_cost_cents=amount, erp_cost_observed_at=cost_at if amount is not None else None,
                        erp_cost_status='ready' if amount is not None else cost_state,
                        maintenance_prices=prices[gid][:3], maintenance_price_status=states[gid],
                        maintenance_price_total=len(prices[gid]), _maintenance_shops=sorted(shops) if 'maintenance_price' in access.fields else [])
        return dict(items=items, total=len(found), search_mode=mode, interpreted_terms=terms,
                    keyword=args.get('keyword'), **({'shop_id': args['shop_id']} if 'shop_id' in args else {}))

    def price_changes(self, args):
        from inventory_price_changes import changes
        path = self.service.root / 'profit_monitor.sqlite3'
        if not path.is_file():
            return dict(outcome='no_batch', items=[], total=0)
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            batch = db.execute("SELECT id,finished FROM runs WHERE kind='price-v2' AND status='completed' ORDER BY started DESC,id DESC LIMIT 1").fetchone()
        if not batch:
            return dict(outcome='no_batch', items=[], total=0)
        info = changes(self.service, {'run_id': batch[0]})
        rows = [{k: r[k] for k in ('name', 'goods_id', 'before_cents', 'after_cents', 'delta_cents', 'warehouse')}
                for r in info['parts'] if r['direction'] in ('up', 'down') and ('goods_id' not in args or r['goods_id'] == args['goods_id'])]
        return dict(items=rows[:10], total=len(rows), batch_id=batch[0], checked_at=batch[1] or info['checked_at'])

    def monitor(self, command, args, db):
        gid, threshold = args['goods_id'], args.get('threshold')
        matches = [json.loads(p) for p, c in db.execute('SELECT p.data,c.data FROM products p LEFT JOIN catalog c ON p.sku=c.sku')
                   if gid in {str(json.loads(p).get('goods_id')), str(json.loads(c).get('goods_id')) if c else ''}]
        if len(matches) > 1:
            raise CommandError(409, 'AMBIGUOUS_TARGET', '存在多条监控配置，请在管理界面核对')
        previous = matches[0] if matches else None
        if command == 'monitor.remove':
            # 精确 ERP 编号，歧义或目录冲突一律不删除；沿用界面的归档和提醒取消规则。
            if not previous:
                return dict(outcome='not_monitored', goods_id=gid)
            if previous.get('goods_id') not in ('', None, gid):
                raise CommandError(409, 'AMBIGUOUS_TARGET', '目录身份与监控配置不一致')
            removed, = self.service._remove_products(db, [previous['sku']], expected={previous['sku']:previous})
            if db.execute('SELECT 1 FROM products WHERE sku=?', (previous['sku'],)).fetchone():
                raise RuntimeError('删除监控读回不一致')
            return dict(removed, goods_id=gid, outcome='removed')
        if command == 'monitor.threshold.set':
            if not previous:
                raise CommandError(404, 'NOT_FOUND', '该商品尚未关注')
            if previous.get('goods_id') not in ('', None, gid):
                raise CommandError(409, 'AMBIGUOUS_TARGET', '目录身份与监控配置不一致')
            product = dict(previous, goods_id=gid, threshold=threshold)
            outcome = 'unchanged' if previous['threshold'] == threshold else 'updated'
        elif previous:
            if previous.get('goods_id') not in ('', None, gid):
                raise CommandError(409, 'AMBIGUOUS_TARGET', '目录身份与监控配置不一致')
            product, outcome = dict(previous, goods_id=gid), 'already_exists'
        else:
            candidates = [r for r in rows_for(db) if str(r.get('goods_id')) == gid]
            if len(candidates) != 1:
                if candidates:
                    raise CommandError(409, 'AMBIGUOUS_TARGET', '同一 ERP 编号存在多个目录 SKU')
                raise CommandError(404, 'NOT_FOUND', '目录中未找到 ERP 商品')
            row, = candidates
            basis = args.get('stock_basis', 'company_able')
            product = dict(sku=row['sku'], name=row['name'], goods_id=gid, category=row.get('category') or '其他',
                           threshold=threshold, stock_basis=basis, enabled=True)
            if basis == 'warehouse_stock':
                options = self.service.get(db, 'warehouse_options:' + gid, {})
                depots = [d for d in options.get('depots', []) if d['id'] == args['warehouse_id']]
                if len(depots) != 1:
                    raise bad('请先在软件中核实该商品的分库列表')
                product.update(warehouse_id=depots[0]['id'], warehouse_name=depots[0]['name'])
            outcome = 'created'
        if outcome in ('created', 'updated'):
            try:
                validated = validate_product(product)
                if previous:
                    # 原记录其余字段原样保留；业务保存逻辑仍处理风险变化。
                    validated = dict(previous, threshold=threshold, goods_id=validated['goods_id'])
                self.service._save_product(db, validated)
            except ValueError as error:
                raise bad(str(error)) from None
            saved = json.loads(db.execute('SELECT data FROM products WHERE sku=?', (product['sku'],)).fetchone()[0])
            if saved['threshold'] != threshold or saved['goods_id'] != gid:
                raise RuntimeError('监控配置读回不一致')
            product = saved
        result = {k: product.get(k) for k in ('name', 'sku', 'goods_id', 'threshold', 'stock_basis', 'warehouse_id', 'warehouse_name', 'enabled', 'alerts')}
        if result['stock_basis'] in (None, 'company_able'):
            result.update(warehouse_name='公司大库', stock_basis='company_able')
        return dict(result, outcome=outcome, previous_threshold=previous['threshold'] if previous else None, requested_threshold=threshold)
