"""Serialized scheduler and isolated query workers. Delivery requires live opt-in."""
from pathlib import Path
from collections import deque
from contextlib import nullcontext
import json
import queue
import subprocess
import sys
import threading
import time

from douyin_inventory import DouyinInventory
from douyin_export import ChromeExportWorker, ChromeMachineExportWorker
from douyin_stock_page import ChromeStockWorker
from douyin_erp import attach_maintenance
from douyin_scope import MACHINE_SCOPE


def worker_command(tool_root, profile):
    client = tool_root / 'app' / 'stock_client.py'
    if not client.is_file():
        raise ValueError('缺少抖店库存查询工具的 app/stock_client.py，请重新安装完整候选包。')
    if getattr(sys, 'frozen', False):
        worker = Path(sys.executable).with_name('douyin_inventory_worker.exe')
        if not worker.is_file():
            raise ValueError('缺少抖店库存查询 worker.exe，请重新安装完整候选包。')
        return [str(worker), str(client), str(profile)]
    python = tool_root / '.venv' / 'Scripts' / 'python.exe'
    worker = Path(__file__).with_name('douyin_inventory_worker.py')
    if not python.is_file():
        raise ValueError('抖店库存查询工具缺少 Python 环境，请运行初始化环境.bat。')
    if not worker.is_file():
        raise ValueError('缺少 douyin_inventory_worker.py，请重新获取完整源码。')
    return [str(python), '-B', '-u', '-X', 'utf8', str(worker), str(client), str(profile)]


class QueryWorker:
    def __init__(self, tool_root, profile):
        command = worker_command(tool_root, profile)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.answers = queue.Queue()
        self.close_lock = threading.Lock()
        def read():
            for line in self.process.stdout:
                try:
                    self.answers.put(json.loads(line))
                except ValueError:
                    pass
            self.answers.put({'ok': False})
        threading.Thread(target=read, daemon=True, name='douyin-query-reader').start()


    def call(self, op, timeout=180):
        self.process.stdin.write(json.dumps({'op': op}) + '\n')
        self.process.stdin.flush()
        try:
            result = self.answers.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise ValueError('全店查询超时，本轮库存未采用。') from None
        if not result.get('ok'):
            raise ValueError('查询未完成，请核验登录状态及库存页面。')
        return result

    def close(self):
        with self.close_lock:
            self._close()

    def _close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write('{"op":"close"}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
        for stream in (self.process.stdin, self.process.stdout):
            if not stream.closed:
                stream.close()


class DouyinRuntime:
    def __init__(self, service, *, live=False, worker_factory=ChromeMachineExportWorker):
        self.store = DouyinInventory(service.root)
        if live:
            self.store.ensure_default_stores()
        self.store.migrate_export()
        self.store.migrate_maintenance()
        self.store.migrate_restock()
        self.service, self.live = service, live
        folder = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        self.tool_root = folder / '抖店库存查询工具'
        self.worker_factory = worker_factory
        self.workers, self.due = {}, {}
        self.commands = queue.Queue()
        self.collection_lock = threading.Lock()
        self.pending_collections = set()
        self.job_lock = threading.Lock()
        self.running_jobs = {}
        self.report_waiting = set()
        self.online_lock = threading.Lock()
        self.max_parallel = 3
        self.stop_event = threading.Event()
        self.notice = '官方表格导出与 ERP 缺货检查；补库存提醒按已启用机器人发送。'
        self.thread = threading.Thread(target=self.run, daemon=True, name='douyin-inventory')
        self.online_startup = [s['id'] for s in self.store.stores(include_deleted=True)] if live else []
        # 恢复上次已提交但未就绪的任务，不为已成功或普通错误店铺自动新建导出。
        if live:
            for config in self.store.stores():
                if config.get('patrol_selected', True) and any(word in config['status'] for word in ('报表仍在生成', '报表生成中')):
                    self.submit('inspect', config['id'])
        self.thread.start()

    def submit(self, operation, ident):
        # 同一店铺的手动、定时导出共用占位，排队和执行中均不重复创建官方报表。
        if isinstance(operation, str) and operation in ('check', 'export', 'confirm', 'inspect'):
            if not self.reserve_collection(ident):
                self.notice = '该店铺已排队或正在检查，本次未重复提交。'
                return False
        self.commands.put((operation, ident))
        self.notice = '操作已排队，正在处理…'
        return True

    def reserve_collection(self, ident):
        """界面线程和调度线程只在短临界区操作占位，不持锁执行网络请求。"""
        with self.collection_lock:
            if ident in self.pending_collections:
                return False
            self.pending_collections.add(ident)
            return True

    def check_all(self):
        """按勾选范围检查，首次店铺在本店队列中先核验身份，再建立库存基线。"""
        result = dict(queued=0, busy=0, skipped=[])
        for config in self.store.stores():
            if not config.get('patrol_selected', True):
                continue
            try:
                self.validate_connection(config)
            except ValueError as error:
                result['skipped'].append(config['name'] + '（' + str(error) + '）')
                continue
            key = 'queued' if self.submit('inspect', config['id']) else 'busy'
            result[key] += 1
        return result

    def toggle_selected(self):
        """批量启停勾选店铺；尚无有效基线的店铺明确跳过，不冒充已启用。"""
        configs = [s for s in self.store.stores() if s.get('patrol_selected', True)]
        # 部分店铺尚无基线时，也必须能暂停其他已启用店铺，不能卡在“启用”状态。
        enable = bool(configs) and not any(s['enabled'] for s in configs)
        result = dict(queued=0, skipped=[], enabled=enable)
        for config in configs:
            if bool(config['enabled']) == enable:
                continue
            if enable and (not config['confirmed'] or not config['checked']):
                result['skipped'].append(config['name'] + '（请先立即检查）')
                continue
            self.submit('enable' if enable else 'pause', config['id'])
            result['queued'] += 1
        return result

    def due_stores(self):
        """取消勾选阻止后续定时排队，已开始的一轮仍独立完成。"""
        return [s for s in self.store.stores() if s['enabled'] and s.get('patrol_selected', True)]

    def validate_connection(self, config):
        """多店必须显式绑定独立扩展连接；同一浏览器账号环境不能冒充不同店铺。"""
        if config.get('connection_mode') == 'managed':
            return
        others = [s for s in self.store.stores() if s['id'] != config['id'] and s['confirmed'] and s.get('connection_mode') != 'managed']
        profile = config.get('browser_profile', '')
        if others and not profile:
            raise ValueError('请在登录设置选择独立连接标识')
        if profile and any(s.get('browser_profile') == profile for s in others):
            raise ValueError('连接标识与其他店铺重复')

    def activity(self, ident):
        """返回当前店铺的排队状态，避免一个店铺的进度覆盖其他店铺。"""
        with self.job_lock:
            if ident in self.running_jobs:
                return ('等待报表，自动续取中' if ident in getattr(self, 'report_waiting', set())
                        else self.store.store(ident)['status'] if '正在' in self.store.store(ident)['status'] else '正在检查或处理设置')
        with self.collection_lock:
            return '操作已排队，等待检查' if ident in self.pending_collections else ''

    def submit_remove(self, ident):
        """删除也进入本店串行队列，不中断其他店铺的检查。"""
        reply = queue.Queue(maxsize=1)
        self.submit(dict(kind='remove_store', reply=reply), ident)
        return reply

    def collect_reserved(self, ident, *, confirm=False):
        """成功和失败都释放占位，并推后下一次定时检查，避免手动后紧接着重导。"""
        try:
            if confirm:
                # 先读到正确店名，才确认新基线；校验失败不清空原有快照。
                self.validate_connection(self.store.store(ident))
                self.worker(ident).call('login')
                self.store.rebase(ident, confirmed=True)
            from douyin_export import ReportPending
            # 等待不阻塞界面，沿用已保存任务；取消勾选或退出即可停止续取。
            while True:
                try:
                    self.collect(ident)
                    break
                except ReportPending:
                    if not hasattr(self, 'report_waiting'):
                        self.report_waiting = set()
                    self.report_waiting.add(ident)
                    self.store.fail(ident, '报表生成中；30 秒后自动续取同一任务，保留历史库存。')
                    if self.stop_event.wait(30):
                        break
                    if not self.store.store(ident).get('patrol_selected', True):
                        self.store.fail(ident, '已取消勾选，停止自动续取；保留历史库存。')
                        break
        finally:
            getattr(self, 'report_waiting', set()).discard(ident)
            try:
                self.due[ident] = time.monotonic() + self.store.store(ident)['interval']
            finally:
                with self.collection_lock:
                    self.pending_collections.discard(ident)

    def submit_exclusions(self, ident, text, *, remove=False, reason='手动标记下架'):
        """名单编辑与采集串行；窗口通过独立队列接收本地保存结果，不等待网络同步。"""
        reply = queue.Queue(maxsize=1)
        self.submit(dict(kind='exclusions', text=text, remove=remove, reason=reason, reply=reply), ident)
        return reply

    def worker(self, ident):
        if ident not in self.workers:
            config = self.store.store(ident)
            factory = self.worker_factory
            if config.get('connection_mode') == 'managed' and factory is ChromeMachineExportWorker:
                from douyin_managed_export import ManagedExportWorker
                factory = ManagedExportWorker
            self.workers[ident] = factory(self.tool_root, self.store.root / 'profiles' / ident)
            self.workers[ident].expected_shop = config['name']
            self.workers[ident].expected_browser = config['browser']
            self.workers[ident].browser_profile = ('managed-' + ident) if config.get('connection_mode') == 'managed' else config['browser_profile']
            self.workers[ident].progress = lambda n, total: self.progress(ident, n, total)
            self.workers[ident].status = lambda text: self.report(ident, text)
        return self.workers[ident]

    def report(self, ident, text):
        """将导出、下载和 ERP 阶段同时写入当前店铺状态。"""
        # 进入下载或 ERP 等新阶段后立即结束等待标记。
        getattr(self, "report_waiting", set()).discard(ident)
        self.notice = text
        with self.store.db() as db:
            db.execute('UPDATE stores SET status=? WHERE id=?', (text, ident))

    def progress(self, ident, n, total):
        self.notice = f'正在读取整机 SKU：{n}/{total} 个链接；保留上次有效库存'
        with self.store.db() as db:
            db.execute('UPDATE stores SET status=? WHERE id=?', (self.notice, ident))

    def collect(self, ident):
        config = self.store.store(ident)
        if not config['confirmed']:
            raise ValueError('请先核对并确认浏览器中的店铺身份与库存口径。')
        self.validate_connection(config)
        self.notice = f"正在导出“{config['name']}”的官方商品表…"
        with self.store.db() as db:
            db.execute('UPDATE stores SET status=? WHERE id=?', (self.notice, ident))
        result = self.worker(ident).call('collect')
        if self.stop_event.is_set():
            return
        # 抖店通知使用独立频道，现有库存群不会自动订阅；未开通时只记录、不积压群消息。
        if result.get('field') in ('dom_stock', 'export_sellable'):
            # 类目必须由采集器逐页核验，未明确属于整机的商品不得进入 ERP 查询。
            if any(p.get('monitor_scope') != MACHINE_SCOPE for p in result['products']):
                raise ValueError('整机范围未核实，本轮未采用；不会查询单品配件。')
            self.notice = '库存已读取，正在读取主机维护表并核对 ERP 实时库存…'
            with self.store.db() as db:
                db.execute('UPDATE stores SET status=? WHERE id=?', (self.notice, ident))
            attach_maintenance(self.service, config, result['products'])
        if self.stop_event.is_set():
            return
        notify = self.notification_ready()
        self.store.accept(ident, config['revision'], result['products'], complete=result['complete'],
                          notify=notify, field=result.get('field'))
        if result.get('export'):
            with self.store.db() as db:
                db.execute('UPDATE stores SET export_file=? WHERE id=?', (result['export']['file'], ident))
        self.notice = self.store.store(ident)['status'] + ('；钉钉提醒已启用' if notify else '；仅记录，不发送群消息')
        online = None
        try:
            online = self.sync_online(ident)
        finally:
            # 在线表失败不能吞掉已完成的库存检查及独立群通知。
            self.notify_current(ident, online_ready=isinstance(online, dict) and online.get('ok') is True)

    def sync_online(self, ident, *, force=False):
        """在线明细同步与群消息发送分开；失败不覆盖成功采集的库存快照。"""
        if not getattr(self, 'live', False):
            return
        from douyin_online import sync_online
        # 三店采集并行，统一明细表的回读及写入仍按批次串行。
        with getattr(self, 'online_lock', nullcontext()):
            return sync_online(self.store, ident, force=force)

    def notification_ready(self):
        """正式运行且机器人明确订阅抖店频道时才允许群通知。"""
        if not getattr(self, 'live', False):
            return False
        from notification_robots import store_for_service
        return any(r['enabled'] and 'douyin_inventory' in r['channels']
                   for r in store_for_service(self.service).list_robots())

    def notify_current(self, ident, *, online_ready=True):
        """只发送本次核对后的补库存清单，不投递旧归零／恢复事件。"""
        if not self.notification_ready():
            return []
        from notification_robots import store_for_service
        from douyin_notification import deliver_current, delivery_label
        robots = store_for_service(self.service)
        result = deliver_current(self.store, robots, ident, online_ready=online_ready)
        self.notice = self.store.store(ident)['status'] + '；群提醒：' + delivery_label(self.store, ident, robots)
        return result

    def recheck_parts(self, ident):
        """复用平台快照更新配件判断，不重新导出，也不改变平台库存采集时间。"""
        config = self.store.store(ident)
        rows = self.store.snapshots(ident)
        if not rows:
            raise ValueError('尚无平台库存，请先完成一次官方导出')
        products = {}
        for row in rows:
            product = products.setdefault(row['product_id'], dict(product_id=row['product_id'], product_name=row['product_name'],
                monitor_scope=row['monitor_scope'], category=row.get('category', ''), product_url=row.get('product_url', ''), skus=[]))
            sku = dict(row); sku[config['field']] = row['quantity']; product['skus'].append(sku)
        self.report(ident, '正在读取维护表并核对 ERP 库存；平台快照时间保持不变')
        attach_maintenance(self.service, config, list(products.values()))
        self.store.accept(ident, config['revision'], list(products.values()), complete=True, notify=False,
                          field=config['field'], now=config['checked'])
        with self.store.db() as db:
            db.execute('UPDATE stores SET revision=revision+1,status=? WHERE id=?',
                       ('配件库存核对完成；平台库存仍为上次导出快照', ident))
        online = None
        try:
            online = self.sync_online(ident)
        finally:
            self.notify_current(ident, online_ready=isinstance(online, dict) and online.get('ok') is True)

    def execute(self, operation, ident):
        if self.store.store(ident).get('deleted') and operation != 'online':
            if isinstance(operation, dict) and operation.get('reply'):
                operation['reply'].put(dict(ok=False, message='该店铺已移出监控'))
            return
        if isinstance(operation, dict) and operation.get('kind') == 'connect':
            self.execute('login', ident)
            operation['reply'].put(dict(ok=True, message='本店连接已校验，可返回抖店页面立即检查'))
        elif isinstance(operation, dict) and operation.get('kind') == 'browser_settings':
            value = operation['value']
            changed = self.store.save_browser(ident, value['browser'], value.get('profile',''), value['mode'])
            if changed:
                previous = self.workers.pop(ident, None)
                if previous:
                    previous.close()
            if operation.get('open'):
                if value['mode'] != 'managed':
                    raise ValueError('现有扩展模式请自行打开已登录的浏览器')
                try:
                    self.worker(ident).call('open')
                except Exception:
                    # 连接方式已经变更，即使浏览器没能启动，也要移除失效的旧提醒。
                    if changed:
                        self.sync_online(ident)
                    raise
                message = '本店专用浏览器已打开，请手动登录后点击“连接店铺”'
            else:
                message = '本店登录设置已保存'
            self.report(ident, message)
            operation['reply'].put(dict(ok=True, message=message))
            if changed:
                self.sync_online(ident)
        elif isinstance(operation, dict) and operation.get('kind') == 'remove_store':
            self.store.remove_store(ident)
            previous = self.workers.pop(ident, None)
            if previous:
                previous.close()
            self.due.pop(ident, None)
            operation['reply'].put(dict(ok=True))
            self.sync_online(ident, force=True)
        elif isinstance(operation, dict) and operation.get('kind') == 'exclusions':
            try:
                result = self.store.update_exclusions(ident, operation['text'], remove=operation['remove'], reason=operation['reason'])
            except Exception as error:
                operation['reply'].put(dict(ok=False, message=str(error) if isinstance(error, ValueError) else '名单保存失败，请检查本机文件权限'))
                return
            self.notice = f"下架排除名单已保存：本店共 {result['total']} 个链接；下次成功检查按新名单提醒"
            operation['reply'].put(dict(ok=True, **result))
            # 云端按已有规则直接删除退出提醒的行；失败保留本地排除，界面可再次同步。
            self.sync_online(ident, force=True)
        elif operation == 'login':
            self.validate_connection(self.store.store(ident))
            previous = self.workers.pop(ident, None)
            if previous:
                previous.close()
            self.worker(ident).call('login')
            # 身份验证失败时保留历史快照；已确认的同店重连不清空已有数据。
            if not self.store.store(ident)['confirmed']:
                self.store.rebase(ident)
            self.notice = '浏览器店铺已核对。勾选参与巡检的店铺后，点击“立即检查”建立基线。'
            self.store.fail(ident, self.notice)
        elif operation.startswith('maintenance:'):
            value = json.loads(operation.split(':', 1)[1])
            self.store.save_maintenance(ident, value['path'], value['sheet'], value['shop_id'])
            self.notice = '维护表已保存，可点击“核对配件库存”'
            self.sync_online(ident)
        elif operation.startswith('browser:'):
            value = json.loads(operation.split(':', 1)[1])
            self.store.save_browser(ident, value['browser'], value.get('profile', ''), value.get('mode','extension'))
            previous = self.workers.pop(ident, None)
            if previous:
                previous.close()
            self.notice = '浏览器设置已保存，请手动登录后连接店铺'
            self.sync_online(ident)
        elif operation == 'parts':
            self.recheck_parts(ident)
        elif operation == 'online':
            self.sync_online(ident, force=True)
        elif operation in ('confirm', 'check', 'export', 'inspect'):
            config = self.store.store(ident)
            if operation == 'inspect' and not config.get('patrol_selected', True):
                # 轮到执行前重新读取勾选，避免取消勾选后仍启动排队中的浏览器。
                with self.collection_lock:
                    self.pending_collections.discard(ident)
                return
            self.collect_reserved(ident, confirm=operation == 'confirm' or
                                  (operation == 'inspect' and not config['confirmed']))
        elif operation in ('enable', 'pause'):
            self.store.enable(ident, operation == 'enable')
            self.due[ident] = 0
            self.notice = '已启用，首轮建立基线。' if operation == 'enable' else '已暂停，不补发暂停期间事件。'
        elif operation.startswith('interval:'):
            interval = int(operation.split(':', 1)[1])
            self.store.update_interval(ident, interval)
            self.due[ident] = time.monotonic() + interval
            self.notice = f'已保存：每 {interval // 60} 分钟导出一次。'
        else:
            raise ValueError('未知操作。')

    def run(self):
        """最多三个店铺并行，每个店铺内部的采集、设置和删除保持先后顺序。"""
        waiting = {}
        try:
            while not self.stop_event.is_set():
                try:
                    operation, ident = self.commands.get(timeout=.1)
                except queue.Empty:
                    operation = ident = None
                if operation:
                    waiting.setdefault(ident, deque()).append(operation)
                with self.job_lock:
                    for key, job in list(self.running_jobs.items()):
                        if not job.is_alive():
                            self.running_jobs.pop(key)
                    active = set(self.running_jobs)
                if self.online_startup:
                    key = self.online_startup.pop(0)
                    waiting.setdefault(key, deque()).append('online')
                for config in self.due_stores():
                    ident = config['id']
                    if (not config['enabled'] or ident in active or waiting.get(ident)
                            or time.monotonic() < self.due.get(ident, 0)):
                        continue
                    if self.reserve_collection(ident):
                        waiting.setdefault(ident, deque()).append('inspect')
                for ident in list(waiting):
                    if self.stop_event.is_set() or len(active) >= self.max_parallel:
                        break
                    if ident in active:
                        continue
                    operation = waiting[ident].popleft()
                    if not waiting[ident]:
                        waiting.pop(ident)
                    job = threading.Thread(target=self.execute_job, args=(operation, ident), daemon=True,
                                           name='douyin-shop-' + ident[:8])
                    with self.job_lock:
                        self.running_jobs[ident] = job
                        job.start()
                    active.add(ident)
                # 下载完成后才清理，三个店铺的有效快照不会在另一任务读取时被移动。
                if not active and not waiting and self.commands.empty():
                    from douyin_export_cleanup import cleanup_due
                    try:
                        self.cleanup_state = cleanup_due(self.store)
                    except Exception:
                        self.cleanup_state = dict(errors=['自动清理未完成，文件已保留；请检查目录权限'])
        finally:
            for worker in list(self.workers.values()):
                worker.close()

    def execute_job(self, operation, ident):
        """一家店的失败只写入本店状态；其余店铺继续执行。"""
        try:
            if not self.stop_event.is_set():
                self.execute(operation, ident)
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else '操作失败；保留历史库存，请检查本店设置。'
            self.store.fail(ident, message)
            if isinstance(operation, dict) and operation.get('reply') and operation['reply'].empty():
                operation['reply'].put(dict(ok=False, message=message))
        finally:
            if isinstance(operation, str) and operation in ('check', 'export', 'confirm', 'inspect'):
                with self.collection_lock:
                    self.pending_collections.discard(ident)

    def close(self):
        self.stop_event.set()
        for worker in list(self.workers.values()):
            worker.close()
        self.thread.join(timeout=3)
        deadline = time.monotonic() + 3
        with self.job_lock:
            jobs = list(self.running_jobs.values())
        for job in jobs:
            job.join(timeout=max(0, deadline - time.monotonic()))
