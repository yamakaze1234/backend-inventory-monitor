"""独立店铺浏览器的官方导出适配：复用报表校验，仅替换页面连接方式。"""
from pathlib import Path
import json
import re
import threading
import time

from douyin_browser import ShopBrowser, PageLoading
from douyin_export import ChromeMachineExportWorker, PRODUCT_URL, REPORT_URL, SHOP_SELECTOR


class ManagedExportWorker(ChromeMachineExportWorker):
    def __init__(self, tool_root, profile):
        self.root = Path(profile).resolve().parent.parent
        self.ident = Path(profile).name
        self.expected_shop = ''
        self.expected_browser = 'chrome'
        self.page = 'managed'
        self.stopped = threading.Event()
        self.browser_profile = 'managed-' + self.ident
        self.download = None
        self.last_navigation = None
        self.last_click = None
        self.poll_interval = .75

    def browser(self):
        if not hasattr(self, '_browser'):
            self._browser = ShopBrowser(self.root, self.ident, self.expected_browser)
        return self._browser

    def close(self):
        # 只停止本工具操作，不退出用户已登录的专用浏览器。
        self.stopped.set()

    def finish_collection(self):
        """成功采集并接受快照后关闭本店窗口，下次巡检按原登录目录重开。"""
        self.browser().close_after_collection()

    def eval(self, expression):
        if self.stopped.is_set():
            raise ValueError('本店检查已停止')
        return self.browser().evaluate(expression)

    def wait(self, expression, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.eval(expression):
                return
            self.pause(.75)
        raise ValueError('页面未就绪或登录失效，请在本店浏览器完成登录后重新连接')

    def navigation_gap(self, seconds=5):
        """页面切换至少间隔五秒，给页面自身的异步工作留出处理时间。"""
        if self.last_navigation is not None:
            remaining = seconds - (time.monotonic() - self.last_navigation)
            if remaining > 0:
                self.pause(remaining)

    def open_page(self, url, ready, *, refresh=False):
        expected = json.dumps(url.split('?', 1)[0])
        same_page = self.eval(f'location.href.split("?")[0] === {expected}')
        previous_origin = None
        if not same_page or refresh:
            self.navigation_gap(15 if refresh else 5)
            if same_page:
                previous_origin = self.eval('performance.timeOrigin')
                self.browser().reload()
            else:
                self.browser().navigate(url)
            self.last_navigation = time.monotonic()
        # 连续三次确认页面、店名和控件就绪；刷新时拒绝接受旧文档的 DOM。
        expression = (f'location.href.split("?")[0] === {expected} && document.readyState === "complete"'
                      f' && !!document.querySelector({json.dumps(ready)})'
                      f' && !!document.querySelector({json.dumps(SHOP_SELECTOR)})?.textContent?.trim()')
        if previous_origin is not None:
            expression += f' && performance.timeOrigin !== {json.dumps(previous_origin)}'
        stable, deadline = 0, time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                stable = stable + 1 if self.eval(expression) else 0
                if stable == 1:
                    self.check_shop()
            except PageLoading:
                stable = 0
            if stable >= 3:
                break
            self.pause(.75)
        else:
            raise ValueError('页面未就绪或登录失效，请在本店浏览器完成登录后重新连接')
        self.check_shop()

    def refresh_reports(self):
        """等待期间仅刷新报表页，取消商品页与报表页的反复往返。"""
        self.open_page(REPORT_URL, 'table', refresh=True)
        self.wait_report_records(seconds=20)

    def before_export_submit(self, pending):
        # 先持久化可能提交的任务，再点击；连接中断也绝不盲目补点。
        self.save_pending(self.root / 'chrome' / (self.ident + '.pending.json'),
                          dict(pending, submission_unknown=True))

    def load_pending(self, path):
        """损坏或身份不符的任务不得当作空记录重建，保留现场等待核对。"""
        try:
            pending = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ValueError('导出任务记录无法核对；未重复提交，请检查本店导出记录') from None
        if pending == {}:
            return pending
        if (not isinstance(pending, dict) or pending.get('shop') != self.expected_shop
                or type(pending.get('count')) is not int or pending['count'] <= 0
                or type(pending.get('started')) not in (int, float) or not pending['started'] > 0
                or not isinstance(pending.get('old'), list)
                or not all(isinstance(item, str) for item in pending['old'])):
            raise ValueError('导出任务记录无法核对；未重复提交，请检查本店导出记录')
        return pending

    def check_pending_progress(self, pending, fresh, path):
        if not pending.get('submission_unknown'):
            return
        if fresh:
            pending.pop('submission_unknown')
            self.save_pending(path, pending)
        elif time.time() - pending['started'] >= 120:
            raise ValueError('导出提交结果未确认；未重复提交，请核对本店官方导出记录')

    def recover_browser(self):
        """上层限制每轮恢复一次；恢复后的下一步仍须校验店铺与原导出任务。"""
        if self.stopped.is_set():
            raise ValueError('本店检查已停止')
        self.report('正在恢复本店浏览器；先核对原导出任务，不重复提交')
        self.pause(5)
        self.browser().recover(REPORT_URL)
        self.last_navigation = time.monotonic()
        self.open_page(REPORT_URL, 'table')

    def click(self, selector, text, *, seconds=15):
        """等待本店唯一控件可操作；列表加载时不误判丢失，成功点击后不重试。"""
        # 用户选择较慢的处理节奏；成功点击也至少间隔一秒，不连续抢操作。
        if self.last_click is not None:
            remaining = 1 - (time.monotonic() - self.last_click)
            if remaining > 0:
                self.pause(remaining)
        self.check_shop()
        if text == '下载报表':
            self.prepare_download()
        expression = '''(() => {
          if(document.querySelector(SHOP)?.textContent?.trim() !== EXPECTED)
            return {state:'wrong_shop'};
          const nodes = [...document.querySelectorAll(SELECTOR)].filter(e =>
            e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && e.textContent.trim() === TEXT);
          if(nodes.length > 1) return {state:'ambiguous',count:nodes.length};
          if(nodes.length === 0) return {state:'waiting',reason:'missing'};
          const node=nodes[0];
          if(node.disabled || node.matches(':disabled') || node.getAttribute('aria-disabled') === 'true')
            return {state:'waiting',reason:'disabled'};
          node.click(); return {state:'clicked'};
        })()'''.replace('SHOP', json.dumps(SHOP_SELECTOR)).replace('EXPECTED', json.dumps(self.expected_shop))
        expression = expression.replace('SELECTOR', json.dumps(selector)).replace('TEXT', json.dumps(text))
        deadline = time.monotonic() + seconds
        while True:
            result = self.eval(expression)
            if not isinstance(result, dict):
                raise ValueError('页面控件状态读取失败，本轮未采用')
            if result.get('state') == 'clicked':
                self.last_click = time.monotonic()
                return
            if result.get('state') == 'wrong_shop':
                raise ValueError('浏览器店铺与配置不一致或登录失效；未点击导出控件')
            if result.get('state') == 'ambiguous':
                raise ValueError('找到多个可见的“' + text + '”，无法确定归属，本轮未采用')
            if result.get('state') != 'waiting':
                raise ValueError('页面控件状态未知，本轮未采用')
            if time.monotonic() >= deadline:
                reason = '仍被禁用' if result.get('reason') == 'disabled' else '尚未显示'
                raise ValueError('等待“' + text + '”可操作超时：控件' + reason + '，本轮未采用')
            # 等待真实控件状态，不延长固定睡眠，也不重放已成功的点击。
            self.pause(.75)

    def prepare_download(self):
        if self.download:
            self.download.close()
        self.download = self.browser().browser_channel()
        self.download_folder = (self.root / 'managed-downloads' / self.ident).resolve()
        if not self.download_folder.is_relative_to(self.root.resolve()):
            raise ValueError('下载目录不属于本店')
        self.download_folder.mkdir(parents=True, exist_ok=True)
        with self.browser().page() as page:
            self.download_frame = page.command('Page.getFrameTree')['frameTree']['frame']['id']
        self.download.command('Browser.setDownloadBehavior', dict(behavior='allowAndName', downloadPath=str(self.download_folder), eventsEnabled=True))

    def wait_download(self, timeout):
        """只认领本店页面、本次点击后收到的下载事件，不按全局文件名猜测归属。"""
        if not self.download:
            raise ValueError('尚未点击本店报表下载')
        deadline, guid = time.monotonic() + timeout, None
        try:
            while time.monotonic() < deadline:
                if self.stopped.is_set():
                    raise ValueError('本店检查已停止')
                try:
                    event = self.download.event(min(1, max(.01, deadline - time.monotonic())))
                except TimeoutError:
                    continue
                data = event.get('params', {})
                if event.get('method') == 'Browser.downloadWillBegin':
                    name = data.get('suggestedFilename', '')
                    if (data.get('frameId') == self.download_frame and name.startswith('productExport-')
                            and Path(name).name == name and name.endswith('.xlsx')
                            and re.fullmatch(r'[a-fA-F0-9-]{32,36}', data.get('guid', ''))):
                        guid = data['guid']
                elif event.get('method') == 'Browser.downloadProgress' and guid and data.get('guid') == guid:
                    if data.get('state') == 'canceled':
                        raise ValueError('本次报表下载已取消，原库存已保留')
                    if data.get('state') == 'completed':
                        source = self.download_folder / guid
                        target = self.download_folder / (guid + '.xlsx')
                        if source.is_symlink() or not source.is_file() or target.exists():
                            raise ValueError('本次下载文件归属未通过校验')
                        # GUID 文件属于当前下载，改为表格后缀后交给原官方表解析器。
                        source.rename(target)
                        return dict(downloaded=True, state='complete', filename=str(target))
            raise ValueError('本店报表下载超时，原库存已保留')
        finally:
            self.download.close()
            self.download = None

    def cli(self, *args, timeout=50, raw=False):
        # 官方导出流程仅需要这两个等待命令，其余页面操作走明确的本店 CDP 通道。
        if args[:2] == ('wait', 'text'):
            self.wait('document.body.innerText.includes(' + json.dumps(args[2]) + ')', min(timeout, 20))
            return {}
        if args[:2] == ('wait', 'download'):
            return self.wait_download(timeout)
        raise ValueError('独立浏览器不支持此操作')

    def call(self, op, timeout=300):
        if op == 'open':
            self.browser().open(PRODUCT_URL)
            return dict(ok=True)
        if op in ('login', 'collect') and not self.browser().running():
            # 只启动本店已隔离的 Chrome/Edge；沿用登录目录，不接管日常浏览器。
            self.report('正在打开本店专用浏览器；登录失效时请在该窗口重新登录')
            destination = REPORT_URL if op == 'collect' else PRODUCT_URL
            ready = 'table' if op == 'collect' else '#exportSearchedGoods'
            self.browser().open(destination)
            self.last_navigation = time.monotonic()
            self.open_page(destination, ready)
        if op == 'login':
            self.open_page(PRODUCT_URL, '#exportSearchedGoods')
            return dict(ok=True)
        try:
            return super().call(op, timeout=timeout)
        finally:
            if self.download:
                self.download.close()
                self.download = None
