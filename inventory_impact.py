"""缺货配件与配置的只读关联；不改 ERP、工作台或抖店库存。"""
from datetime import datetime, timezone
import json
import re
import sqlite3
import time

from profit_manifest import read_manifest


def published_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (ValueError, TypeError):
        return 0


class InventoryImpact:
    def __init__(self, service):
        self.service = service

    def import_manifest(self, path):
        manifest = read_manifest(path)
        if not published_time(manifest['publishedAt']):
            raise ValueError('配置发布时间须包含时区。')
        with self.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.service.get(db, 'impact_manifest')
            if old:
                if (old['sourceId'], old['workspaceId']) != (manifest['sourceId'], manifest['workspaceId']):
                    raise ValueError('配置来源或工作区不同，请先解除当前配置关联。')
                if manifest['revision'] < old['revision'] or (
                    manifest['revision'] == old['revision'] and manifest['contentHash'] != old['contentHash']):
                    raise ValueError('配置版本回退或同版本内容不同，保留原关联。')
            scope = self.service.get(db, 'scope')
            if not scope:
                raise ValueError('请先完成一次 ERP 查询，再导入配置快照。')
            old_scope = self.service.get(db, 'impact_scope')
            if old_scope and old_scope != scope:
                raise ValueError('ERP 来源已变化，请先解除当前配置关联。')
            self.service.put(db, 'impact_manifest', manifest)
            self.service.put(db, 'impact_scope', scope)
            self.service.put(db, 'impact_path', str(path))
        return len(manifest['configs'])

    def unbind(self):
        # 只解除本功能的关联，原业务数据和已导入文件都保留。
        with self.service.db() as db:
            for key in ('impact_manifest', 'impact_scope', 'impact_path', 'impact_mappings'):
                db.execute('DELETE FROM meta WHERE key=?', (key,))

    def shop_data(self):
        """只读本机已采集库存，不启动登录、采集线程或通知。"""
        path = self.service.root / 'douyin_inventory' / 'inventory.sqlite3'
        if not path.is_file():
            return [], {}
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            stores = [dict(r) for r in db.execute('SELECT id,name,field,enabled,confirmed,revision,interval,checked,status FROM stores')]
            rows = {(r['store'], r['product'], r['sku']): dict(r) for r in db.execute('SELECT store,product,sku,quantity,observed,data FROM snapshots')}
            return stores, rows
        finally:
            db.close()

    def bind_listing(self, shop_id, config_id, *, spu, store_id, product_id, sku_id):
        if not str(spu).strip():
            raise ValueError('请输入明确的 SPU 编号。')
        if not all(re.fullmatch(r'[1-9][0-9]{0,30}', str(v)) for v in (product_id, sku_id)):
            raise ValueError('抖店商品 ID 和 SKU ID 须为完整数字编号。')
        stores, _ = self.shop_data()
        store = next((s for s in stores if s['id'] == store_id and s['confirmed']), None)
        if not store:
            raise ValueError('请在抖店库存页完成该店铺的身份核验。')
        with self.service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            manifest = self.service.get(db, 'impact_manifest')
            if not manifest or not any(c['shopId'] == shop_id and c['configId'] == config_id for c in manifest['configs']):
                raise ValueError('该配置已不在当前快照中，请刷新列表。')
            maps = self.service.get(db, 'impact_mappings', [])
            # 一个工作台店铺不能同时指向不同的本机店铺。
            if any((m['shop_id'] == shop_id and m['store_id'] != store_id)
                   or (m['store_id'] == store_id and m['shop_id'] != shop_id) for m in maps):
                raise ValueError('该工作台店铺已关联其他抖店，请先解除配置关联。')
            if any(m['store_id'] == store_id and m['product_id'] == product_id and m['sku_id'] == sku_id
                   and (m['shop_id'], m['config_id']) != (shop_id, config_id) for m in maps):
                raise ValueError('该店铺商品和 SKU 已关联其他配置，请核对。')
            maps = [m for m in maps if (m['shop_id'], m['config_id']) != (shop_id, config_id)]
            maps.append(dict(shop_id=shop_id, config_id=config_id, spu=str(spu).strip(), store_id=store_id,
                             product_id=product_id, sku_id=sku_id, store_revision=store['revision']))
            self.service.put(db, 'impact_mappings', maps)

    def report(self, status=None, now=None):
        now = time.time() if now is None else now
        status = self.service.status() if status is None else status
        with self.service.db() as db:
            manifest = self.service.get(db, 'impact_manifest')
            scope = self.service.get(db, 'impact_scope')
            current_scope = self.service.get(db, 'scope')
            mappings = self.service.get(db, 'impact_mappings', [])
        if not manifest:
            return dict(bound=False, rows=[], summary='导入 DIY 工作台的完整配置快照，查看低库存配件影响哪些配置。')
        stores, snapshots = self.shop_data()
        stores = {s['id']: s for s in stores}
        maps = {(m['shop_id'], m['config_id']): m for m in mappings}
        age = now - published_time(manifest['publishedAt'])
        fresh = 0 <= age <= 86400
        erp_fresh = status.get('enabled') and status.get('connection') == 'online' and scope == current_scope
        products = {p.get('goods_id'): p for p in status['products'] if p.get('goods_id') and p.get('enabled')}
        rows, invalid = [], 0
        for config in manifest['configs']:
            affected, uncertain = [], []
            for part in config['parts']:
                gid = part.get('goodsId')
                if not re.fullmatch(r'[1-9][0-9]{0,19}', str(gid or '')):
                    invalid += 1
                    uncertain.append(f"{part['name']}：ERP 编号未关联")
                    continue
                product = products.get(gid)
                if not product:
                    uncertain.append(f"{part['name']}：未纳入库存关注")
                    continue
                qty = product.get('monitor_qty' if product.get('stock_basis') == 'warehouse_stock' else 'able')
                stock_age = now - product.get('observed_at', 0)
                if (not erp_fresh or not 0 <= stock_age <= status.get('interval_seconds', 7200) + 60
                    or product.get('data_error') or product.get('risk') == 'unknown' or qty is None):
                    uncertain.append(f"{part['name']}：库存待核对")
                elif qty <= product['threshold']:
                    affected.append(dict(goods_id=gid, name=part['name'], quantity=qty, threshold=product['threshold'],
                                         warehouse=product.get('warehouse_name') or '公司大库', qty_per_config=part.get('qty')))
            if not affected and not uncertain:
                continue
            mapping = maps.get((config['shopId'], config['configId']))
            listing, priority, shop_qty = '店铺库存待核对', '待核对', None
            if mapping:
                store = stores.get(mapping['store_id'])
                snap = snapshots.get((mapping['store_id'], mapping['product_id'], mapping['sku_id']))
                # 重新登录、切换店铺、暂停或失败后，旧库存不能支撑“仍有可用库存”的判断。
                valid = (store and snap and store['confirmed'] and store['enabled']
                         and store['revision'] == mapping['store_revision'] and store['status'].startswith('查询成功')
                         and 0 <= now - snap['observed'] <= store['interval'] * 2 + 60)
                if valid and snap['quantity'] is not None:
                    shop_qty = snap['quantity']
                    if store['field'] == 'dom_stock':
                        data = json.loads(snap['data'])
                        listing = f"{data.get('stock_state', '页面库存')} {shop_qty}；商家后台口径"
                        priority = '优先核对' if affected and erp_fresh and fresh and shop_qty > 0 else ('平台库存为零' if shop_qty == 0 else priority)
                    elif store['field'] in ('unoccupied_stock', 'export_sellable'):
                        # 官方导出含预售和下架商品，只用于核对优先级，不推断前台可购买。
                        listing = (f'导出表库存 {shop_qty}（含预售）；售卖状态待核对'
                                   if store['field'] == 'export_sellable' else f'未占用库存 {shop_qty}')
                        if affected and erp_fresh and fresh and shop_qty > 0:
                            priority = '优先核对'
                        elif shop_qty == 0:
                            priority = '平台库存为零'
                    else:
                        listing = f'总库存 {shop_qty}；可用库存待核对'
            if not fresh:
                listing += '；配置快照已过期' if age > 86400 else '；配置发布时间异常'
            rows.append(dict(shop_id=config['shopId'], config_id=config['configId'],
                             product=config['product'], name=config['name'], spu=mapping['spu'] if mapping else '',
                             parts=affected, uncertain=uncertain, priority=priority, listing=listing,
                             mapping=mapping, shop_quantity=shop_qty))
        rows.sort(key=lambda r: (r['priority'] != '优先核对', not bool(r['parts']), r['shop_id'], r['product'], r['name']))
        return dict(bound=True, rows=rows, fresh=fresh, revision=manifest['revision'], published_at=manifest['publishedAt'],
                    total=len(manifest['configs']), invalid_parts=invalid,
                    summary=f"配置快照 v{manifest['revision']} · {len(manifest['configs'])} 个配置 · {sum(bool(r['parts']) for r in rows)} 个受影响 · {sum(r['priority']=='优先核对' for r in rows)} 个优先核对",
                    scope_matches=scope == current_scope)
