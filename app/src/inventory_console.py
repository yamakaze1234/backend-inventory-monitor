"""统一 ReUI 工作台的本机服务；复用库存实例与原订单匹配队列。"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from inventory_updater import InventoryUpdater
from inventory_version import VERSION
from order_bot_adapter import OrderBotAdapter
from project_paths import UI_ROOT


class InventoryConsole:
    def __init__(self, service, native_action=None, bot=None, updater=None, assets=None, port=0, business=None):
        self.service = service
        self.native_action = native_action
        self.business = business
        self.bot = bot or OrderBotAdapter(workspace_root=service.root)
        if business is not None:business.order_bot=self.bot
        self.updater = updater or InventoryUpdater(service.root)
        self.assets = Path(assets or UI_ROOT / 'console').resolve()
        if not (self.assets / 'index.html').is_file():
            raise ValueError('工作台资源缺失，请先运行 dev.ps1 WebBuild')
        self.token = secrets.token_urlsafe(32)
        # 机器人令牌只允许数据读取，不可调用业务操作或软件更新。
        self.query_token = secrets.token_urlsafe(32)
        self.action_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.cached = None
        self.cached_at = 0
        self.update_thread = None
        self.stop_thread = None
        self.stop_error = ''
        self.owns_bot = False
        self.closing = False
        self.window = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value, content_type='application/json; charset=utf-8'):
                raw = value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(raw)

            def trusted(self, read_only=False):
                # 同源、固定 Host 以及自定义请求头共同阻止外部网页调用写接口。
                return (self.headers.get('Host') == urlsplit(outer.url).netloc
                    and self.headers.get('Origin') in (None,outer.url)
                    and (secrets.compare_digest(self.headers.get('X-Console-Token',''),outer.token) or
                        read_only and secrets.compare_digest(self.headers.get('X-Console-Token',''),outer.query_token)))

            def do_GET(self):
                if self.headers.get('Host') != urlsplit(outer.url).netloc:
                    return self.reply(403,{'error':'来源不允许'})
                route = urlsplit(self.path)
                try:
                    if route.path.startswith('/api/'):
                        if not self.trusted(read_only=route.path in ('/api/data/catalog','/api/data/job')):
                            return self.reply(403,{'error':'来源不允许'})
                        query = parse_qs(route.query)
                        if route.path == '/api/data/job' and outer.business:
                            result=outer.business.job(query.get('id',[''])[0])
                            if result.get('module')!='profit' or result.get('action')!='view-changes':return self.reply(403,dict(error='不允许读取该任务'))
                            return self.reply(200,result)
                        if route.path == '/api/data/catalog':
                            from inventory_data_api import catalog
                            return self.reply(200,catalog())
                        if route.path == '/api/data/connection':
                            return self.reply(200,dict(url=outer.url,token=outer.query_token,scope='read-only',expires='process-exit'))
                        if route.path == '/api/business/product-search' and outer.business:
                            # 纯目录查询，不发起采集或写入 ERP。
                            term=query.get('query',[''])[0].strip()
                            if len(term)>100:raise ValueError('搜索词最多 100 字')
                            rows=outer.service.catalog(term,limit=51,mode='goods_id' if term.isascii() and term.isdecimal() else 'name') if term else []
                            watched=outer.service.products()
                            skus={p['sku'] for p in watched};goods={str(p.get('goods_id','')) for p in watched}
                            return self.reply(200,dict(more=len(rows)>50,rows=[dict(sku=r['sku'],goods_id=str(r.get('goods_id','')),name=r['name'],watched=r['sku'] in skus or str(r.get('goods_id','')) in goods) for r in rows[:50]]))
                        if route.path == '/api/business/idle':
                            return self.reply(200,dict(state='idle'))
                        if route.path == '/api/business/job' and outer.business:
                            return self.reply(200,outer.business.job(query.get('id',[''])[0]))
                        if route.path == '/api/business/download' and outer.business:
                            path=outer.business.reviewed(query.get('key',[''])[0],'download')
                            return self.reply(200,path.read_bytes(),'application/octet-stream')
                        if route.path.startswith('/api/business/view/') and outer.business:
                            return self.reply(200,outer.business.snapshot(route.path.rsplit('/',1)[-1],{k:v[0] for k,v in query.items()}))
                        if route.path == '/api/inventory':
                            return self.reply(200,outer.inventory())
                        if route.path == '/api/orders':
                            state=outer.bot.snapshot(int(query.get('page',['0'])[0]),int(query.get('size',['25'])[0]))
                            state['stopError']=outer.stop_error
                            return self.reply(200,state)
                        if route.path == '/api/orders/detail':
                            return self.reply(200,outer.bot.action('detail',{'id':query.get('id',[''])[0]}))
                        if route.path == '/api/update':
                            return self.reply(200,outer.updater.snapshot())
                        if route.path == '/api/info':
                            with outer.service.db() as db:
                                auto = outer.service.get(db,'console_auto_update_check',True)
                            from inventory_workspace_config import workspace_settings
                            return self.reply(200,dict(version=VERSION,autoCheck=auto,business=outer.business is not None,workspace=workspace_settings(outer.service)))
                        return self.reply(404,{'error':'接口不存在'})
                    if route.path == '/app-icon.ico':
                        return self.reply(200,(UI_ROOT/'monitor_assets/monitor.ico').read_bytes(),'image/x-icon')
                    path = (outer.assets / (route.path.lstrip('/') or 'index.html')).resolve()
                    if not path.is_relative_to(outer.assets) or not path.is_file():
                        return self.reply(404,{'error':'页面不存在，请先构建前端'})
                    self.reply(200,path.read_bytes(),mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                except (ValueError,TypeError) as error:
                    self.reply(400,{'error':str(error)})
                except Exception:
                    self.reply(503,{'error':'数据暂时无法读取，请重试或查看本机运行记录'})

            def do_POST(self):
                if not self.trusted(read_only=urlsplit(self.path).path in ('/api/data/query','/api/data/refresh')) or self.headers.get_content_type() != 'application/json':
                    return self.reply(403,{'error':'来源不允许'})
                self.connection.settimeout(15)
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0 < length <= (2_000_000 if urlsplit(self.path).path == '/api/business/action' else 32768):
                        return self.reply(413,{'error':'请求体大小无效'})
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict):
                        raise ValueError('请求必须是对象')
                    if urlsplit(self.path).path == '/api/data/query':
                        from inventory_data_api import query,DataError
                        try:return self.reply(200,query(outer,body))
                        except DataError as error:return self.reply(400,dict(error=str(error),code=error.code))
                    if not outer.action_lock.acquire(False):
                        return self.reply(409,{'error':'操作正在处理中，请稍后重试'})
                    try:
                        result=outer.action(urlsplit(self.path).path,body)
                    finally:
                        outer.action_lock.release()
                    self.reply(200,result)
                except (ValueError,TypeError) as error:
                    self.reply(400,{'error':str(error)})
                except Exception:
                    self.reply(503,{'error':'操作未确认完成，请先刷新记录，不要重复提交'})

        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        self.server.daemon_threads=True
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True,name='inventory-console')
        self.thread.start()

    def inventory(self):
        """短缓存合并多页面请求，只返回界面需要的字段，避免输出连接配置。"""
        with self.cache_lock:
            if self.cached is None or time.monotonic()-self.cached_at > 2:
                state=self.service.status()
                fields=('sku','name','goods_id','threshold','enabled','able','stock','purchase','monitor_qty','risk','observed_at','warehouse_name')
                self.cached={key:state.get(key) for key in ('enabled','connection','checked_at','next_check','source_mode','source_busy')}
                self.cached['products']=[{key:p.get(key) for key in fields} for p in state['products']]
                self.cached_at=time.monotonic()
            return self.cached

    def action(self,path,body):
        if self.closing:
            raise ValueError('程序正在安全退出，暂不接收新操作')
        if path == '/api/data/refresh':
            if not self.business or self.updater.state['phase'] in ('preparing','installing'):
                raise ValueError('业务服务未就绪或更新中')
            if set(body)-{'dataset','request_id'} or body.get('dataset')!='price_changes':
                raise ValueError('仅支持查看价格变动')
            return self.business.submit('profit','view-changes',{},body.get('request_id'))
        if path == '/api/business/action' and self.business:
            return self.business.submit(body.get('module'),body.get('action'),body.get('values'),body.get('requestId'))
        if path == '/api/orders/action':
            if self.updater.state['phase'] in ('preparing','installing'):
                raise ValueError('更新安装中，暂不接收新任务')
            action=body.get('action')
            if self.stop_thread and self.stop_thread.is_alive():
                raise ValueError('正在等待订单工作进程安全停止，请稍后操作')
            result=self.bot.action(action,body)
            if action == 'start':
                self.owns_bot=True
            if action == 'stop':
                self.stop_error=''
                def settle_stop():
                    try:
                        deadline=time.monotonic()+90
                        while not self.bot.action('finish-stop').get('stopped'):
                            if time.monotonic()>deadline:
                                raise ValueError('后台仍在安全停止中，请稍后再次点击停止服务')
                            time.sleep(1)
                        self.owns_bot=False
                    except Exception as error:
                        self.stop_error=str(error)
                self.stop_thread=threading.Thread(target=settle_stop,daemon=True,name='order-safe-stop')
                self.stop_thread.start()
            return result
        if path == '/api/inventory/check':
            self.service.request_check()
            return {'message':'已请求检查，结果以最新采集时间为准'}
        if path == '/api/inventory/enabled':
            if not isinstance(body.get('enabled'),bool):
                raise ValueError('监控开关无效')
            self.service.set_enabled(body['enabled'])
            self.cached=None
            return {'message':'库存监控开关已保存'}
        if path == '/api/update/preferences':
            if not isinstance(body.get('autoCheck'),bool):
                raise ValueError('更新偏好无效')
            with self.service.db() as db:
                self.service.put(db,'console_auto_update_check',body['autoCheck'])
            return {'message':'更新偏好已保存'}
        if path == '/api/update/check':
            return self.update_job(self.updater.check)
        if path == '/api/update/install':
            if self.business and self.business.busy():
                raise ValueError('业务操作尚未结束，请等待结果后更新')
            if not self.native_action:
                raise ValueError('此运行方式不能安装更新')
            # 先冻结原匹配服务并等它确认停止，再退出库存程序；不强杀 ERP 工作进程。
            state=self.bot.snapshot()
            if state.get('active',0):
                raise ValueError('存在排队、执行中或待核验的匹配任务，请处理完成后更新')
            def update():
                self.updater.download()
                self.native_action('install-update')
            return self.update_job(update)
        raise ValueError('不支持的工作台操作')

    def update_job(self,callback):
        if self.update_thread and self.update_thread.is_alive():
            raise ValueError('更新任务正在进行')
        def run():
            try:
                callback()
            except Exception as error:
                self.updater.state.update(phase='error',error=str(error))
        self.update_thread=threading.Thread(target=run,daemon=True,name='console-update')
        self.update_thread.start()
        return {'message':'更新任务已开始'}

    def open(self):
        import ctypes
        from ctypes import wintypes
        # 会话令牌只放 URL fragment，不进入 HTTP 请求或服务日志。
        if self.window is not None and self.window.poll() is None:
            # 托盘重复打开时激活现有窗口，不重复启动 WebView2 和页面轮询。
            @ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
            def activate(hwnd,_):
                pid=wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
                if pid.value == self.window.pid and ctypes.windll.user32.IsWindowVisible(hwnd):
                    ctypes.windll.user32.ShowWindow(hwnd,9)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                return True
            ctypes.windll.user32.EnumWindows(activate,0)
            return
        host=UI_ROOT/'console-host/InventoryConsoleHost.exe'
        if not host.is_file():
            raise ValueError('桌面窗口资源缺失，请先构建 console-host')
        self.window=subprocess.Popen([str(host),self.url+'/#'+self.token,str(os.getpid())],
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def preview(root,desktop=False,duration=None,check_host=False):
    """隔离演示可用于浏览器验收，不连接真实 ERP 或订单队列。"""
    from inventory_sources import InventoryRuntimeService
    from inventory_business import BusinessConsole
    from douyin_inventory_runtime import DouyinRuntime
    from types import SimpleNamespace
    service=InventoryRuntimeService(root)
    service.offline_preview=True
    runtime=DouyinRuntime(service,live=False)
    controller=SimpleNamespace(root=Path(root),status=lambda:dict(state='paused',ready=0,sources=[]))
    business=BusinessConsole(service,controller,runtime,offline=True)
    with service.db() as db:service.put(db,'daily_source_root',str(Path(root).resolve()))
    # 样例店铺身份仅供隔离验收；和正式部门配置没有关联。
    demo_shops=[dict(id=ident,name=name) for ident,name in zip(('76','32','33'),('英特尔旗舰店（样例）','技嘉旗舰店（样例）','乔思伯旗舰店（样例）'))]
    if not runtime.store.stores():
        from douyin_scope import MACHINE_SCOPE
        for index,shop in enumerate(demo_shops):
            name=shop['name']
            ident=runtime.store.add_store(name)
            runtime.store.rebase(ident,confirmed=True)
            with runtime.store.db() as db:
                db.execute('UPDATE stores SET maintenance_shop_id=? WHERE id=?',(shop['id'],ident))
            config=runtime.store.store(ident)
            runtime.store.accept(ident,config['revision'],[dict(product_id=str(90000+index),product_name=name+'整机',monitor_scope=MACHINE_SCOPE,
                skus=[dict(sku_id=str(91000+index),sku_name='标准配置',export_sellable=0,components={'现货可售':0,'预售库存':0,'阶梯库存':0},
                    erp=dict(state='配件齐全',parts=[],shortages=[],unknown=[]))])],complete=True,notify=False)
    class DemoBot:
        def configuration(self):
            # 隔离验收仅展示未配置状态，不连接任何真实订单后端。
            return dict(configured=False,deployed=False,shops=[],target={},message="尚未配置本部门订单后端")
        def snapshot(self,page=0,size=25):
            rows=[dict(id=f'demo-{i}',kind='order',number=f'10987654321000{i:04}',shop=['76','32','33'][i%3],
                       state=['queued','running','verified','needs_review'][i%4],created=1794000000-i*60,delivery='pending') for i in range(83)]
            return dict(available=True,workerOnline=False,schedulePaused=True,serviceEnabled=False,
                        active=0,total=len(rows),shops=demo_shops,jobs=rows[page*size:(page+1)*size])
        def action(self,action,payload=None):
            if action=='detail':
                return {'replyText':'隔离样例结果：本次验收没有执行 ERP 写入或发送群消息。'}
            raise ValueError('隔离预览不执行真实匹配')
    if not service.products():
        for i in range(120):
            service.save_product(dict(sku=f'DEMO-{i:04}',name=['英特尔处理器','技嘉显卡','乔思伯机箱'][i%3]+f' · 样例 {i+1}',threshold=5,enabled=True))
    # 隔离样例目录用于验证按名称和编号添加。
    with service.db() as db:
        db.execute('INSERT OR REPLACE INTO catalog VALUES (?,?)',('REUI-TEST',json.dumps(dict(sku='REUI-TEST',goods_id='12345',name='ReUI 隔离测试商品'))))
    # 仅隔离候选模式提供可重复的成本变化样例，真实 SQL 与发送均未接入。
    from profit_manifest import sha
    monitor=business.profit()
    class PreviewCosts:
        step=0
        def read_profit_costs(self,ids):
            self.step+=1
            return dict(costs={gid:10000+self.step*100 for gid in ids},failed=[],sourceScope='isolated-preview',collectedAt=time.time())
    source=PreviewCosts();monitor.source_provider=lambda:source
    manifest=dict(contract='profit-monitor/v1',schemaVersion=1,complete=True,formulaVersion='profit/v1',sourceId='preview',workspaceId='preview',revision=1,publishedAt='2026-11-08T00:00:00+08:00',configs=[dict(shopId='76',configId='preview-config',product='样例整机链接',name='样例双内存配置',priceCents=100000,feeCents=0,installment=0,parts=[dict(lineId='memory',slot='内存',name='样例内存',goodsId='123',qty=2,taxCents=10000)])])
    manifest['businessHash']=sha(manifest['configs']);manifest['contentHash']=sha(manifest)
    fixture=Path(root)/'price-preview-manifest.json';fixture.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    monitor.bind(fixture);monitor.check(allow_notifications=False);business.preview_prices=True
    server=InventoryConsole(service,bot=DemoBot(),business=business)
    address=server.url+'/#'+server.token
    (Path(root)/'preview-url.txt').write_text(address,encoding='utf-8')
    # 打包窗口程序没有 stdout；仅源码验收输出本地预览地址。
    import sys
    if sys.stdout is not None:
        print(address,flush=True)
    if desktop:
        server.open()
    if check_host:
        # 仅检查打包运行库和 WebView2 Runtime，不创建桌面窗口。
        subprocess.run([str(UI_ROOT/'console-host/InventoryConsoleHost.exe'),'--self-check',
                        str(Path(root).resolve()/'host-check.txt')],check=True,timeout=30,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        threading.Event().wait(duration)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        runtime.close()
        service.close_source()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--preview-data',type=Path,required=True)
    preview(parser.parse_args().preview_data)
