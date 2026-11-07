"""Independent, durable Douyin zero-stock transitions; no network by default."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
import uuid


FIELDS = {'export_sellable': '现货＋预售＋阶梯库存',
          'dom_stock': '在售 SKU 未占用库存（无活动占用）',
          'unoccupied_stock': '未占用库存（旧版）', 'total_stock': '总库存（旧版）'}


def stock_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return int(text) if text.isascii() and text.isdigit() else None


def exact_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('商品或 SKU 缺少有效 ID，整轮结果未采用。')
    value = str(value).strip()
    if not value:
        raise ValueError('商品或 SKU 缺少有效 ID，整轮结果未采用。')
    return value


class DouyinInventory:
    def __init__(self, root):
        self.root = Path(root) / 'douyin_inventory'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'inventory.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS stores (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, field TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0, interval INTEGER NOT NULL DEFAULT 300,
                    checked REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '待登录核验');
                CREATE TABLE IF NOT EXISTS snapshots (
                    store TEXT, product TEXT, sku TEXT, data TEXT NOT NULL,
                    quantity INTEGER, observed REAL, PRIMARY KEY(store,product,sku));
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, store TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, status TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS restock_previews (store TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS restock_records (
                    id TEXT PRIMARY KEY, store TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS excluded_products (
                    store TEXT NOT NULL, product TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(store,product));
                CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            # 新字段只记录官方导出的文件位置，不改变现有 ERP 数据库。
            if 'export_file' not in [r[1] for r in db.execute('PRAGMA table_info(stores)')]:
                db.execute("ALTER TABLE stores ADD COLUMN export_file TEXT NOT NULL DEFAULT ''")
            # 每店独立设置，不从旧匹配工程自动继承维护表或浏览器登录绑定。
            columns = {r[1] for r in db.execute('PRAGMA table_info(stores)')}
            if 'deleted' not in columns:
                db.execute('ALTER TABLE stores ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0')
            for name, default in (('maintenance_path', ''), ('maintenance_sheet', ''), ('maintenance_shop_id', ''),
                                  ('browser', 'chrome'), ('browser_profile', ''), ('online_url', '')):
                if name not in columns:
                    db.execute(f"ALTER TABLE stores ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            if 'connection_mode' not in columns:
                # 已确认店铺保留现有连接；尚未配置的店铺默认使用自动隔离的专用浏览器。
                db.execute("ALTER TABLE stores ADD COLUMN connection_mode TEXT NOT NULL DEFAULT 'extension'")
                for candidate in db.execute("SELECT id FROM stores WHERE confirmed=0 AND checked=0 AND browser_profile='' AND deleted=0").fetchall():
                    ident = candidate['id']
                    # 重新连接会暂时清除确认标记；已有页面绑定或历史记录的店铺不自动切换。
                    if ((self.root / 'chrome' / (ident + '.json')).exists()
                            or db.execute('SELECT 1 FROM restock_records WHERE store=? LIMIT 1', (ident,)).fetchone()):
                        continue
                    db.execute("UPDATE stores SET connection_mode='managed' WHERE id=?", (ident,))
            # 升级后不补发旧归零／恢复任务，历史记录仍可回看。
            db.execute("UPDATE events SET status='cancelled' WHERE status IN ('pending','failed','partial')")

    def _clear_parts(self, db, ident, reason):
        """换维护表后保留平台库存，清除旧配件结论，防止显示另一来源的缺货判断。"""
        db.execute('DELETE FROM restock_previews WHERE store=?', (ident,))
        for item in db.execute('SELECT product,sku,data FROM snapshots WHERE store=?', (ident,)).fetchall():
            row = json.loads(item['data'])
            row['erp'] = dict(source='maintenance', state='待核对', shortages=[], parts=[], unknown=[reason], checked=0)
            db.execute('UPDATE snapshots SET data=? WHERE store=? AND product=? AND sku=?',
                       (json.dumps(row, ensure_ascii=False), ident, item['product'], item['sku']))

    def migrate_maintenance(self):
        """旧 ERP 套餐结果不能冒充维护表结果，升级时仅清除这些过期结论。"""
        with self.db() as db:
            for config in db.execute('SELECT * FROM stores').fetchall():
                rows = db.execute('SELECT data FROM snapshots WHERE store=?', (config['id'],)).fetchall()
                if any(json.loads(r[0]).get('erp', {}).get('source') != 'maintenance' for r in rows):
                    self._clear_parts(db, config['id'], '请先为本店选择主机维护表，再核对配件库存')
                    db.execute('UPDATE stores SET revision=revision+1 WHERE id=?', (config['id'],))

    def save_maintenance(self, ident, path, sheet, shop_id):
        """先校验用户选定文件，再保存本店绑定；不修改维护表。"""
        from douyin_maintenance import KNOWN_SHOPS, read_maintenance
        config = self.store(ident)
        expected = KNOWN_SHOPS.get(config['name'])
        if expected and expected != str(shop_id):
            raise ValueError('维护表的店铺编号与当前店铺不符')
        data = read_maintenance(path, sheet, shop_id)
        with self.db() as db:
            self._clear_parts(db, ident, '维护表设置已更换，请重新核对配件库存')
            db.execute('UPDATE stores SET maintenance_path=?,maintenance_sheet=?,maintenance_shop_id=?,revision=revision+1,status=? WHERE id=?',
                       (data['source'], sheet, str(shop_id), '维护表已保存；点击“核对配件库存”即可使用已有平台快照', ident))

    def save_browser(self, ident, browser, profile='', mode='extension'):
        """只保存浏览器选择和扩展连接标识，登录在浏览器里由用户自己完成。"""
        if browser not in ('chrome', 'edge') or mode not in ('managed', 'extension'):
            raise ValueError('请选择 Chrome 或 Edge')
        profile = str(profile).strip() if mode == 'extension' else ''
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT browser,browser_profile,connection_mode FROM stores WHERE id=? AND deleted=0', (ident,)).fetchone()
            if current is None:
                raise ValueError('店铺不存在')
            if tuple(current) == (browser, profile, mode):
                return False
            if profile and db.execute("SELECT 1 FROM stores WHERE id<>? AND deleted=0 AND connection_mode='extension' AND browser_profile=?", (ident, profile)).fetchone():
                raise ValueError('该扩展连接已绑定其他店铺，请为本店使用独立浏览器用户配置。')
            db.execute('DELETE FROM restock_previews WHERE store=?', (ident,))
            db.execute('UPDATE stores SET browser=?,browser_profile=?,connection_mode=?,confirmed=0,enabled=0,revision=revision+1,status=? WHERE id=?',
                       (browser, profile, mode, '浏览器设置已保存，请手动登录后连接店铺', ident))
        return True

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def stores(self, *, include_deleted=False):
        with self.db() as db:
            # 勾选使用已有偏好表保存，不增加数据库字段；旧店铺默认参与巡检。
            return [dict(r) for r in db.execute("SELECT s.*, COALESCE(p.value,'1')='1' AS patrol_selected "
                "FROM stores s LEFT JOIN preferences p ON p.key='patrol_selected:' || s.id" +
                ('' if include_deleted else ' WHERE s.deleted=0') + ' ORDER BY s.rowid')]

    def select_patrol(self, ident, selected):
        """逐店保存巡检勾选，切换查看店铺和重启均不会改变选择。"""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM stores WHERE id=? AND deleted=0', (ident,)).fetchone():
                raise ValueError('店铺已移出监控。')
            db.execute('INSERT INTO preferences(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('patrol_selected:' + ident, '1' if selected else '0'))

    def ensure_default_stores(self):
        """首次启用三店配置时补齐常用店铺；删除后不在下一次启动时自动加回。"""
        from douyin_maintenance import KNOWN_SHOPS
        from douyin_online import binding
        online = binding(self.root)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM preferences WHERE key='default_shops_v1'").fetchone():
                return
            names = {r[0] for r in db.execute('SELECT name FROM stores')}
            for name in KNOWN_SHOPS:
                if name not in names:
                    db.execute('INSERT INTO stores(id,name,field,interval,online_url,connection_mode) VALUES (?,?,?,?,?,?)',
                               (uuid.uuid4().hex, name, 'export_sellable', 1800, online['url'] if online else '', 'managed'))
            db.execute("INSERT INTO preferences VALUES ('default_shops_v1','1')")

    def remove_store(self, ident):
        """移出监控并使进行中的旧结果失效；历史文件保留，不执行永久文件删除。"""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE stores SET deleted=1,enabled=0,confirmed=0,revision=revision+1,status='已移出监控' WHERE id=?", (ident,))
            db.execute('DELETE FROM restock_previews WHERE store=?', (ident,))
            db.execute("UPDATE events SET status='cancelled' WHERE store=? AND status IN ('pending','failed','partial')", (ident,))

    def add_store(self, name, field='export_sellable', interval=1800):
        name = str(name).strip()
        if not name or len(name) > 80 or field not in FIELDS:
            raise ValueError('请输入店铺名称并选择库存口径。')
        interval = int(interval)
        if not 60 <= interval <= 86400:
            raise ValueError('检查间隔须在 60 至 86400 秒之间。')
        ident = uuid.uuid4().hex
        from douyin_online import binding
        online = binding(self.root)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM stores WHERE name=? AND deleted=0', (name,)).fetchone():
                raise ValueError('该店铺已存在，请在列表中选择；不同店铺请填写完整后台名称。')
            db.execute('INSERT INTO stores(id,name,field,interval,online_url,connection_mode) VALUES (?,?,?,?,?,?)',
                       (ident, name, field, interval, online['url'] if online else '', 'managed'))
        return ident

    def update_interval(self, ident, interval):
        """调整频率不清空基线；官方导出最短五分钟，避免频繁创建报表。"""
        interval = int(interval)
        if not 300 <= interval <= 86400:
            raise ValueError('导出间隔须在 300 至 86400 秒之间。')
        with self.db() as db:
            db.execute('UPDATE stores SET interval=? WHERE id=?', (interval, ident))

    def migrate_export(self):
        """旧 API 库存与导出库存口径不同，升级后暂停旧店铺并重新核验。"""
        for config in self.stores():
            if config['field'] not in ('export_sellable', 'dom_stock'):
                self.rebase(config['id'])
                with self.db() as db:
                    db.execute("UPDATE stores SET field='export_sellable',interval=MAX(interval,300),status='已切换官方导出，请连接 Chrome 并重新确认店铺' WHERE id=?", (config['id'],))

    def store(self, ident):
        with self.db() as db:
            row = db.execute("SELECT s.*, COALESCE(p.value,'1')='1' AS patrol_selected "
                "FROM stores s LEFT JOIN preferences p ON p.key='patrol_selected:' || s.id WHERE s.id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError('店铺不存在。')
        return dict(row)

    def rebase(self, ident, *, confirmed=False):
        """Explicit account/login boundary; discard pending work, retain history."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE stores SET enabled=0,confirmed=?,revision=revision+1,checked=0,status=? WHERE id=?',
                       (int(confirmed), '待首次查询' if confirmed else '待登录核验', ident))
            db.execute('DELETE FROM snapshots WHERE store=?', (ident,))
            db.execute('DELETE FROM restock_previews WHERE store=?', (ident,))
            db.execute("UPDATE events SET status='cancelled' WHERE store=? AND status IN ('pending','failed','partial')", (ident,))

    def enable(self, ident, enabled):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT confirmed,checked FROM stores WHERE id=?', (ident,)).fetchone()
            if row is None or (enabled and (not row['confirmed'] or not row['checked'])):
                raise ValueError('请先核对店铺身份并完成一次库存查询。')
            # Resume from a fresh baseline; do not report changes made while paused.
            db.execute('UPDATE stores SET enabled=?,revision=revision+1,status=? WHERE id=?',
                       (int(enabled), '等待建立监控基线' if enabled else '已暂停', ident))
            db.execute('DELETE FROM snapshots WHERE store=?', (ident,))
            db.execute('DELETE FROM restock_previews WHERE store=?', (ident,))
            db.execute("UPDATE events SET status='cancelled' WHERE store=? AND status IN ('pending','failed','partial')", (ident,))

    def fail(self, ident, reason='查询失败或登录失效；保留历史库存，请重新核验登录'):
        with self.db() as db:
            db.execute('UPDATE stores SET status=? WHERE id=?', (reason, ident))

    def accept(self, ident, revision, products, *, complete, now=None, notify=True, field=None):
        if complete is not True or not isinstance(products, list):
            raise ValueError('全店采集未完成，未更新库存。')
        now = time.time() if now is None else now
        store = self.store(ident)
        field = field or store['field']
        if field not in FIELDS:
            raise ValueError('未知库存口径。')
        rows, seen, product_ids = [], set(), set()
        for product in products:
            pid = exact_id(product.get('product_id'))
            if pid in product_ids or not isinstance(product.get('skus'), list):
                raise ValueError('商品分页重复或 SKU 数据结构不完整。')
            product_ids.add(pid)
            for sku in product['skus']:
                sid = exact_id(sku.get('sku_id'))
                if (pid, sid) in seen:
                    raise ValueError('SKU 重复，未更新本轮库存。')
                seen.add((pid, sid))
                rows.append(dict(product_id=pid, sku_id=sid,
                                 product_name=str(product.get('product_name') or ''),
                                 monitor_scope=product.get('monitor_scope', ''),
                                 category=product.get('category', ''), product_url=product.get('product_url', ''),
                                 sku_name=str(sku.get('sku_name') or ''),
                                 quantity=stock_number(sku.get(field)),
                                 components=sku.get('components', {}),
                                 source=sku.get('source', 'export'),
                                 sale_status=sku.get('sale_status', ''), sku_status=sku.get('sku_status', ''),
                                 stock_state=sku.get('stock_state', '导出库存；售卖状态未核对'),
                                 erp=sku.get('erp', {})))
        created = []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM stores WHERE id=?', (ident,)).fetchone()
            if current is None or current['revision'] != revision or not current['confirmed']:
                raise ValueError('店铺配置已变更或尚未核验，本轮数据未采用。')
            old = {(r['product'], r['sku']): dict(r) for r in db.execute('SELECT * FROM snapshots WHERE store=?', (ident,))}
            # 两种来源的数量口径不同，首次切换仅建立基线，避免伪造售罄/补货。
            if current['field'] != field:
                old = {}
            for row in rows:
                quantity = row['quantity']
                prior = old.get((row['product_id'], row['sku_id']))
                # 从全商品切到整机时重新建立基线；不能把范围变化记成售罄或补货。
                previous = (prior['quantity'] if prior and json.loads(prior['data']).get('monitor_scope', '') == row['monitor_scope'] else None)
                # 只比较相邻两次有效数据；首次、缺行、未知数量都不触发变化提醒。
                kind = ('sold_out' if previous is not None and previous > 0 and quantity == 0 else
                        'restocked' if previous == 0 and quantity is not None and quantity > 0 else None)
                if current['enabled'] and kind:
                    event = dict(row, store_id=ident, store_name=current['name'], field=field,
                                 previous=previous, observed=now, kind=kind)
                    eid = uuid.uuid4().hex
                    db.execute('INSERT INTO events(id,store,data,created,status) VALUES (?,?,?,?,?)',
                               (eid, ident, json.dumps(event, ensure_ascii=False), now, 'preview'))
                    created.append(eid)
            # Missing rows/quantities explicitly break continuity, never become zero.
            db.execute('DELETE FROM snapshots WHERE store=?', (ident,))
            for row in rows:
                db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?)',
                           (ident, row['product_id'], row['sku_id'], json.dumps(row, ensure_ascii=False), row['quantity'], now))
            unknown = sum(r['quantity'] is None for r in rows)
            db.execute('UPDATE stores SET checked=?,status=?,field=? WHERE id=?',
                       (now, f'查询成功：{len(rows)} 个 SKU，{unknown} 个库存未知；' +
                        ('官方导出，上下架未核对' if field == 'export_sellable' else '页面库存'), field, ident))
            # 库存归零／恢复仅作轨迹；补库存提醒按当前条件独立汇总，首轮也能发现。
            from douyin_restock import sync_preview
            sync_preview(db, dict(current, field=field), rows, inventory_at=now)
        return created

    def low_stock_rules(self, ident):
        from douyin_low_stock import load
        with self.db() as db:
            return load(db, ident)

    def save_low_stock_rules(self, ident, value):
        """规则与预览同事务保存；不改变平台快照和 ERP 核对时间。"""
        from douyin_low_stock import validate
        from douyin_restock import sync_preview
        rules = validate(value)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            config = db.execute('SELECT * FROM stores WHERE id=?', (ident,)).fetchone()
            if config is None:
                raise ValueError('店铺不存在')
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)',
                       ('low_stock:' + ident, json.dumps(rules, ensure_ascii=False)))
            if config['confirmed']:
                rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM snapshots WHERE store=?', (ident,))]
                sync_preview(db, dict(config), rows, inventory_at=config['checked'])
        return rules

    def restock_preview(self, ident):
        """提供当前店铺的本地机器人文案，不访问机器人服务。"""
        with self.db() as db:
            config = db.execute('SELECT confirmed FROM stores WHERE id=?', (ident,)).fetchone()
            row = db.execute('SELECT data FROM restock_previews WHERE store=?', (ident,)).fetchone()
        return json.loads(row[0]) if row and config and config['confirmed'] else None

    def excluded_products(self, ident):
        """按店铺读取名单；尚未出现在导出表中的商品也保留排除设置。"""
        with self.db() as db:
            names = {}
            for row in db.execute('SELECT product,data FROM snapshots WHERE store=?', (ident,)):
                if row['product'] not in names:
                    names[row['product']] = json.loads(row['data']).get('product_name', '')
            return [dict(product_id=r['product'], product_name=names.get(r['product'], '尚未出现在本店导出表'),
                         reason=r['reason'], created=r['created']) for r in db.execute(
                'SELECT product,reason,created FROM excluded_products WHERE store=? ORDER BY created DESC,product', (ident,))]

    def update_exclusions(self, ident, text, *, remove=False, reason='手动标记下架'):
        """名单和当前提醒在一个事务内更新；导出、重启、切店铺均不会清空名单。"""
        from douyin_exclusions import parse_products
        from douyin_restock import sync_preview
        products = parse_products(text)
        reason = str(reason).strip() or '手动标记下架'
        if len(reason) > 120:
            raise ValueError('备注最多 120 个字符。')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            config = db.execute('SELECT * FROM stores WHERE id=?', (ident,)).fetchone()
            if config is None:
                raise ValueError('店铺不存在，请重新选择。')
            changed = 0
            for product in products:
                if remove:
                    changed += db.execute('DELETE FROM excluded_products WHERE store=? AND product=?', (ident,product)).rowcount
                else:
                    changed += db.execute('INSERT OR IGNORE INTO excluded_products VALUES (?,?,?,?)',
                                          (ident,product,reason,time.time())).rowcount
            if changed:
                # 拒绝已在途的旧轮次覆盖；不改平台库存与 ERP 核对时间。
                db.execute('UPDATE stores SET revision=revision+1 WHERE id=?', (ident,))
                if config['confirmed']:
                    rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM snapshots WHERE store=?', (ident,))]
                    sync_preview(db, dict(config), rows, inventory_at=config['checked'])
            total = db.execute('SELECT count(*) FROM excluded_products WHERE store=?', (ident,)).fetchone()[0]
        return dict(changed=changed, total=total, requested=len(products), remove=remove)

    def migrate_restock(self):
        """升级时用已有有效快照生成预览，沿用原核对时间，不伪装成新的实时检查。"""
        from douyin_restock import sync_preview
        from douyin_online import binding
        online = binding(self.root)
        with self.db() as db:
            if online:
                db.execute('UPDATE stores SET online_url=? WHERE online_url<>?', (online['url'], online['url']))
            for config in db.execute('SELECT * FROM stores WHERE confirmed=1').fetchall():
                prior = db.execute('SELECT data FROM restock_previews WHERE store=?', (config['id'],)).fetchone()
                if prior and json.loads(prior[0]).get('format_version') == 4 and (not online or online['url'] in (json.loads(prior[0]).get('payload') or {}).get('markdown',{}).get('text','')):
                    continue
                rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM snapshots WHERE store=?', (config['id'],))]
                if rows:
                    observed = max([config['checked']] + [r.get('erp', {}).get('checked', 0) for r in rows])
                    sync_preview(db, dict(config), rows, inventory_at=config['checked'], detected_at=observed)

    def restock_records(self, ident, limit=100):
        """历史文案按店铺隔离；仅作回看，不进入待发送队列。"""
        with self.db() as db:
            return [dict(json.loads(r['data']), id=r['id']) for r in db.execute(
                'SELECT id,data FROM restock_records WHERE store=? ORDER BY created DESC,rowid DESC LIMIT ?', (ident, limit))]

    def snapshots(self, ident):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM snapshots WHERE store=? ORDER BY product,sku', (ident,))]

    def events(self, limit=200):
        with self.db() as db:
            return [dict(json.loads(r['data']), id=r['id'], status=r['status'])
                    for r in db.execute('SELECT * FROM events ORDER BY created DESC LIMIT ?', (limit,))]

    def deliver(self, sender, *, now=None):
        """当前仅本地预览；即使误调用投递入口，也不能发送旧归零／恢复记录。"""
        return []
