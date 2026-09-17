"""Exclusive SQL/script collection with shared products and unchanged delivery."""
import json
import re
import time
from inventory_monitor import InventoryService
from inventory_sql_service import SqlInventoryService, SqlWorker
from sql_inventory_source import SqlSource


class InventoryRuntimeService(SqlInventoryService):
    def __init__(self, root):
        super().__init__(root)
        self.sql_worker = SqlWorker(self)

    def source_mode(self):
        with self.db() as db:
            return self.get(db, 'inventory_source_mode', 'sql')

    def collector_enabled(self):
        return self.source_mode() == 'script'

    def _transition(self, mode, scope):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old_mode = self.get(db, 'inventory_source_mode', 'sql')
            old_scope = self.get(db, 'scope')
            if old_scope and not old_scope.startswith('sql:'):
                self.put(db, 'script_bound_scope', old_scope)
            if mode == old_mode and scope == old_scope:
                return
            # Keep old snapshots for audit; never compare across collection sources.
            db.execute('CREATE TABLE IF NOT EXISTS source_snapshot_archive (id INTEGER PRIMARY KEY, source TEXT, switched REAL, sku TEXT, data TEXT)')
            db.execute('INSERT INTO source_snapshot_archive(source,switched,sku,data) SELECT ?,?,sku,data FROM snapshots',
                       (old_scope or old_mode, time.time()))
            db.execute('DELETE FROM snapshots')
            db.execute('DELETE FROM journal_baselines')
            db.execute("DELETE FROM meta WHERE key LIKE 'sql_pending:%'")
            skus = [r[0] for r in db.execute('SELECT sku FROM products')]
            for key, value in dict(inventory_source_mode=mode, scope=scope, source_baseline_skus=skus,
                                   checked_at=0, next_check=0, last_skus=[], heartbeat=0, connection_error='',
                                   warehouse_requests=[], erp_native_request={}, erp_connection_alert={'armed':False},
                                   config_revision=self.get(db,'config_revision',0)+1).items():
                self.put(db,key,value)
        self._sql_session_ready = False
        self._sql_session_error = ''

    def configure_sql(self, settings, password, remember=False, connector=None):
        clean = {k:settings[k] for k in ('server','database','user','port')}
        if not password:
            with self.db() as db:
                previous = self.get(db,'sql_connection',{})
            if previous != clean:
                raise ValueError('连接信息已改变，请重新输入数据库密码。')
            from monitor_secret_store import unprotect
            try:
                password = unprotect((self.root/'sql_connection.dat').read_bytes()).decode('utf-8')
            except Exception:
                raise ValueError('请重新输入数据库密码。') from None
        source = SqlSource(clean,password,connector)
        with self.sql_worker.lock:
            if self.sql_worker.busy:
                raise ValueError('正在读取库存，请本轮完成后切换来源。')
            with self.db() as db:
                bound = self.get(db,'sql_bound_scope')
            if bound and bound != source.scope:
                raise ValueError('SQL 来源已绑定，请使用原服务器、数据库和查询账号。')
            encrypted = None
            if remember:
                from monitor_secret_store import protect
                encrypted = protect(password.encode('utf-8'))
            self._transition('sql',source.scope)
            with self.db() as db:
                self.put(db,'sql_connection',clean)
                self.put(db,'sql_remember',bool(remember))
            if encrypted is not None:
                (self.root/'sql_connection.dat').write_bytes(encrypted)
            elif (self.root/'sql_connection.dat').exists():
                from inventory_recycle import recycle_file
                recycle_file(self.root/'sql_connection.dat')
            self.sql_worker.configure(source)

    def use_script(self):
        with self.sql_worker.lock:
            if self.sql_worker.busy:
                raise ValueError('正在读取库存，请本轮完成后切换来源。')
            with self.db() as db:
                scope = self.get(db,'script_bound_scope')
                current = self.get(db,'scope')
                if current and not current.startswith('sql:'): scope = current
            self._transition('script',scope)
            self.sql_worker.message = '脚本查询已启用，请保持 ERP 页面和采集脚本运行。'

    def start_source(self):
        if self.source_mode() == 'sql':
            with self.db() as db:
                settings = self.get(db,'sql_connection',{})
                remember = self.get(db,'sql_remember',False)
            if settings and remember:
                try:
                    self.configure_sql(settings,'',remember=True)
                except Exception:
                    self.sql_worker.message = '数据库自动连接未启动，请在采集设置中重新输入密码。'
        self.sql_worker.start()

    def close_source(self):
        self.sql_worker.stop_event.set()
        if self.sql_worker.thread:
            self.sql_worker.thread.join(timeout=2)

    def status(self):
        if self.source_mode() == 'script':
            result = InventoryService.status(self)
        else:
            result = super().status()
        result['source_mode'] = self.source_mode()
        result['source_message'] = self.sql_worker.message
        result['source_busy'] = self.sql_worker.busy
        return result

    def store_catalog(self, rows, details, scope):
        super().store_catalog(rows, details, scope)
        with self.db() as db:
            self.put(db,'sql_bound_scope',scope)

    def adapt_sql_details(self, details):
        # Retain existing numeric script IDs when the same goods/warehouse name is verified.
        for p in self.products():
            d = details.get(p['sku'])
            if not d or d['goods_id'] != p.get('goods_id') or p.get('stock_basis') != 'warehouse_stock':
                continue
            for depot in d['depots']:
                if depot['name'] == p['warehouse_name']:
                    depot['id'] = p['warehouse_id']
        return details

    def prepare_script_lookup(self):
        if self.pending_warehouse_request(): return
        for p in self.products():
            if p['enabled'] and p.get('stock_basis') == 'warehouse_stock' and p.get('warehouse_id','').startswith('sqlname:'):
                with self.db() as db:
                    retry = self.get(db,'script_lookup_retry:'+p['sku'],0)
                if time.time() >= retry:
                    self.request_warehouses(p['sku'],p['goods_id'])
                    with self.db() as db:
                        self.put(db,'script_lookup_retry:'+p['sku'],time.time()+300)
                    return

    def accept_warehouses(self, request_id, details, *, scope):
        if self.source_mode() == 'sql':
            return super().accept_warehouses(request_id,details,scope=scope)
        from inventory_warehouses import WarehouseLookups
        result = WarehouseLookups.accept_warehouses(self,request_id,details,scope=scope)
        if result['status'] == 'ready':
            for p in self.products():
                if p.get('goods_id') != str(details.get('goods_id')) or not p.get('warehouse_id','').startswith('sqlname:'):
                    continue
                matches = [d for d in result['depots'] if d['name']==p['warehouse_name'] and re.fullmatch(r'[1-9][0-9]*',d['id'])]
                if len(matches)==1:
                    self.save_product(dict(p,warehouse_id=matches[0]['id']))
        return result

    def connection_update(self, error=None, now=None):
        if self.source_mode() == 'script':
            return InventoryService.connection_update(self,error,now)
        return super().connection_update(error,now)
