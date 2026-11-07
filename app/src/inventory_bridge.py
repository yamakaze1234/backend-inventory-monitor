"""Loopback-only collector transport. Never proxies arbitrary ERP requests."""
import json
import threading
import time
import uuid
from inventory_warehouses import ConfigurationChanged
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ORIGIN = 'https://cqzs.3cerp.com'
CLIENT = 'erp-userscript-v1'
PORT = 18763
MAX_BODY = 8 * 1024 * 1024


class Bridge:
    def __init__(self, service, port=PORT, live_delivery=False):
        self.service = service
        self.lock = threading.Lock()
        self.lease = None
        self.retry_at = 0
        self.failures = 0
        self.delivery = None
        self.native = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, data):
                raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                if self.headers.get('Origin') == ORIGIN:
                    self.send_header('Access-Control-Allow-Origin', ORIGIN)
                    self.send_header('Vary', 'Origin')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)

            def do_OPTIONS(self):
                if self.headers.get('Origin') != ORIGIN:
                    self.reply(403, {'error':'来源不允许'})
                    return
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', ORIGIN)
                self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Inventory-Client')
                self.end_headers()

            def do_GET(self):
                self.reply(405, {'error':'请通过库存采集脚本连接'})

            def do_POST(self):
                self.connection.settimeout(15)
                origin = self.headers.get('Origin')
                host = self.headers.get('Host', '')
                expected_hosts = {f'127.0.0.1:{outer.server.server_port}', f'localhost:{outer.server.server_port}'}
                if (origin not in (None, ORIGIN) or self.headers.get('X-Inventory-Client') != CLIENT
                        or host not in expected_hosts or self.headers.get_content_type() != 'application/json'):
                    self.reply(403, {'error':'来源不允许'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= MAX_BODY:
                        self.reply(413, {'error':'请求体大小无效'})
                        return
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError('请求必须为对象')
                    with outer.lock:
                        result = outer.handle(self.path, body)
                    self.reply(200, result)
                except (ValueError, KeyError, TypeError) as error:
                    self.reply(400, {'error':str(error)[:300]})
                except Exception:
                    self.reply(500, {'error':'本机库存服务暂时不可用'})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if live_delivery:
            try:
                from inventory_delivery import start_delivery
                self.delivery = start_delivery(service, live=True)
            except Exception:
                with service.db() as db:
                    service.put(db, 'delivery_error', '通知服务未启动，请检查钉钉配置。')

    def handle(self, path, body):
        now = time.time()
        if hasattr(self.service, 'collector_enabled') and not self.service.collector_enabled():
            self.lease = None
            if path == '/poll':
                return {'check_id':None, 'next_check':now+60, 'source_mode':'sql'}
            if path in ('/recovery-config', '/recovery', '/error'):
                return {'ok':True, 'enabled':False, 'status':'sql_mode'}
            raise ValueError('当前使用 SQL 查询，脚本结果不写入库存。')
        client, scope = str(body.get('client_id', '')), str(body.get('scope', ''))
        if not client or len(client)>100:
            raise ValueError('采集会话标识缺失')
        if path == '/error':
            # Only the active collector can release its lease.
            if self.lease and client == self.lease['client_id']:
                self.lease = None
                self.failures += 1
                self.retry_at = now + min(1800, 30 * 2**min(self.failures, 6))
            self.service.connection_update('ERP 采集失败，请检查登录状态和公司大库页面。')
            return {'ok':True}
        if body.get('warehouse') != '公司大库' or not scope or len(scope)>300:
            raise ValueError('请先在 ERP 分库库存页核对公司大库和当前账号。')
        with self.service.db() as db:
            existing_scope = self.service.get(db, 'scope')
        if existing_scope and existing_scope != scope:
            raise ValueError('ERP 账号或仓库发生变化，已停止接收。')
        if not existing_scope and hasattr(self.service, 'collector_enabled'):
            with self.service.db() as db:
                self.service.put(db,'scope',scope)
                self.service.put(db,'script_bound_scope',scope)
        if path == '/recovery-config':
            with self.service.db() as db:
                enabled = self.service.get(db, 'erp_native_enabled', False) is True
                if existing_scope:
                    previous = self.service.get(db, 'erp_native_request', {})
                    self.service.put(db, 'erp_native_request', {'enabled':enabled,
                        'client_id':client,'at':now,'state':previous.get('state','observe') if previous.get('client_id')==client else 'observe'})
                native = self.service.get(db, 'erp_native_state', {})
            return {'enabled':enabled,'status':native.get('status','waiting')}
        if path == '/recovery':
            state = body.get('state')
            if state not in ('captcha', 'exhausted', 'retrying', 'autofill', 'native_ready'):
                raise ValueError('自动恢复状态无效')
            with self.service.db() as db:
                self.service.put(db, 'erp_recovery', {'state': state, 'at': now})
                if existing_scope:
                    self.service.put(db, 'erp_native_request', {'enabled':self.service.get(db,'erp_native_enabled',False) is True,
                        'client_id':client,'at':now,'state':state})
            return {'ok': True}
        if path == '/poll':
            self.service.connection_update()
            with self.service.db() as db:
                revision = self.service.get(db, 'config_revision', 0)
            status = self.service.status()
            if self.lease and self.lease['expires_at'] > now:
                return {'check_id':None, 'next_check':status['next_check'], 'busy':True}
            self.lease = None
            if hasattr(self.service, 'prepare_script_lookup'):
                self.service.prepare_script_lookup()
            lookup = self.service.pending_warehouse_request()
            exceptions = [dict(sku=p['sku'], goods_id=p.get('goods_id', ''), warehouse_id=p['warehouse_id'],
                               warehouse_name=p['warehouse_name']) for p in status['products']
                          if p['enabled'] and p.get('stock_basis') == 'warehouse_stock'
                          and not p.get('warehouse_id','').startswith('sqlname:')]
            if (lookup or exceptions) and 'warehouse_details_v1' not in body.get('capabilities', []):
                message = '分库特例需要新版采集脚本，请安装 inventory-erp.user.js 0.2.0 或更新版本。'
                self.service.connection_update(message)
                return {'check_id': None, 'collector_error': message, 'next_check': now + 60}
            if lookup:
                self.lease = dict(check_id=uuid.uuid4().hex, client_id=client, scope=scope,
                                  expires_at=now+180, started_at=now, job_type='warehouse_lookup',
                                  request_id=lookup['request_id'], goods_id=lookup['goods_id'])
                return dict(self.lease)
            if not status['enabled'] or max(status['next_check'], self.retry_at) > now:
                return {'check_id':None, 'next_check':max(status['next_check'],self.retry_at)}
            self.lease = dict(check_id=uuid.uuid4().hex, client_id=client, scope=scope,
                              expires_at=now+300, started_at=now, config_revision=revision)
            return dict(self.lease, skus=[p['sku'] for p in status['products'] if p['enabled']],
                        warehouse_products=exceptions,
                        journal_since={p['sku']: p.get('observed_at') or now-86400 for p in status['products'] if p['enabled']})
        if path == '/renew':
            if not self.lease or client != self.lease['client_id'] or body.get('check_id') != self.lease['check_id'] or self.lease['expires_at'] < now:
                raise ValueError('采集任务已过期。')
            self.lease['expires_at'] = now+300
            self.service.connection_update()
            return {'ok':True}
        if path in ('/snapshot', '/warehouse-result'):
            lease = self.lease
            if (not lease or lease['expires_at'] < now or client != lease['client_id']
                    or scope != lease['scope'] or body.get('check_id') != lease['check_id']):
                raise ValueError('采集任务已过期或属于其他页面。')
            if path == '/warehouse-result':
                if lease.get('job_type') != 'warehouse_lookup' or body.get('request_id') != lease.get('request_id'):
                    raise ValueError('分库查询任务不匹配。')
                self.service.accept_warehouses(lease['request_id'], body.get('details'), scope=scope)
                self.lease = None
                self.service.connection_update('')
                return {'ok': True}
            if lease.get('job_type') == 'warehouse_lookup':
                raise ValueError('请更新采集脚本后读取分库明细。')
            if not self.service.status()['enabled']:
                self.lease = None
                raise ValueError('库存监控已暂停。')
            if body.get('complete') is not True:
                raise ValueError('采集未完成，已保留上次快照。')
            rows = body.get('rows')
            if not isinstance(rows, list) or len(rows)>100000:
                raise ValueError('库存数据无效。')
            try:
                self.service.accept_snapshot(lease['check_id'], rows, scope=scope,
                                             warehouse='公司大库', observed_at=now, journals=body.get('journals'),
                                             warehouse_details=body.get('warehouse_details'), config_revision=lease['config_revision'])
            except ConfigurationChanged:
                self.lease = None
                self.retry_at = 0
                return {'ok': False, 'retry': True, 'next_check': 0}
            self.lease = None
            self.failures = 0
            self.retry_at = 0
            return {'ok':True, 'next_check':self.service.status()['next_check']}
        raise ValueError('未知库存操作。')

    def close(self):
        if self.native:
            self.native.close()
        if self.delivery:
            self.delivery.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def start_bridge(service, *, port=PORT, live_delivery=True):
    return Bridge(service, port=port, live_delivery=live_delivery)
