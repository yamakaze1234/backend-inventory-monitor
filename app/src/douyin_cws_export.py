"""CWS 试点页面适配：复用官方报表完整性和整机类目校验。"""
from pathlib import Path
import time

from douyin_export import ChromeExportWorker, PRODUCT_URL, SHOP_SELECTOR
from douyin_managed_export import ManagedExportWorker


class CwsExportWorker(ManagedExportWorker):
    """页面、登录与下载由 CWS 持有；不打开原工作台浏览器。"""

    def __init__(self, script, state_root, shop):
        super().__init__(None, Path(state_root) / 'profiles' / 'cws-gigabyte')
        self.script = script
        self.expected_shop = shop
        self.expected_browser = ''
        self.machine_only = True
        self.download_path = None

    def eval(self, expression):
        return self.script.page.evaluate(expression)

    def pause(self, seconds):
        # Playwright 需持续泵送事件，不能用 Event.wait 阻塞下载回调。
        self.script.page.wait_for_timeout(seconds * 1000)

    def open_page(self, url, ready, *, refresh=False):
        self.navigation_gap(15 if refresh else 5)
        self.script.page.goto(url, wait_until='domcontentloaded', timeout=60000)
        self.last_navigation = time.monotonic()
        self.script.page.locator(ready).first.wait_for(state='visible', timeout=45000)
        self.script.page.locator(SHOP_SELECTOR).first.wait_for(state='visible', timeout=45000)
        self.check_shop()

    def prepare_download(self):
        # CWS 的 download 已注册本次页面下载事件，不能改写全局下载行为。
        pass

    def click(self, selector, text, *, seconds=15):
        if text == '下载报表':
            self.download_path = self._download(
                lambda: super(CwsExportWorker, self).click(selector, text, seconds=seconds))
        else:
            super().click(selector, text, seconds=seconds)

    def _download(self, trigger):
        """CWS 的 no_defaults CDP 连接未开启完成事件，显式限定本页和超时。

        文件仍落在 CWS 注入的本轮 downloads_dir；不读取公共下载目录。
        本试点只串行运行一个 Profile，不与页面调试会话同时采集。
        """
        folder = Path(self.script.downloads_dir).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        page_session = self.script.page.context.new_cdp_session(self.script.page)
        session = self.script.page.context.browser.new_browser_cdp_session()
        frame = page_session.send('Page.getFrameTree')['frameTree']['frame']['id']
        context_id = page_session.send('Target.getTargetInfo')['targetInfo'].get('browserContextId')
        scope = {'browserContextId': context_id} if context_id else {}
        state = {}

        def begun(event):
            if event.get('frameId') == frame:
                state.update(guid=event['guid'], name=event.get('suggestedFilename', ''))

        def progress(event):
            if event.get('guid') == state.get('guid'):
                state['status'] = event.get('state')

        session.on('Browser.downloadWillBegin', begun)
        session.on('Browser.downloadProgress', progress)
        try:
            session.send('Browser.setDownloadBehavior', dict(behavior='allowAndName', downloadPath=str(folder), eventsEnabled=True, **scope))
            trigger()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if state.get('status') == 'completed':
                    if Path(state['name']).suffix.lower() != '.xlsx':
                        raise ValueError('官方报表下载文件格式变化，本轮未采用')
                    source = folder / state['guid']
                    target = folder / (state['guid'] + '.xlsx')
                    source.replace(target)
                    return target
                if state.get('status') == 'canceled':
                    raise ValueError('官方报表下载被取消')
                self.pause(.2)
            raise ValueError('官方报表下载超时，保留原导出任务')
        finally:
            try:
                if state.get('guid') and state.get('status') not in ('completed', 'canceled'):
                    session.send('Browser.cancelDownload', dict(guid=state['guid'], **scope))
                session.send('Browser.setDownloadBehavior', dict(behavior='default', eventsEnabled=False, **scope))
            finally:
                session.detach()
                page_session.detach()

    def cli(self, *args, timeout=50, raw=False):
        if args[:2] == ('wait', 'download') and self.download_path:
            return dict(downloaded=True, state='complete', filename=str(self.download_path))
        return super().cli(*args, timeout=timeout, raw=raw)

    def call(self, op, timeout=300):
        if op == 'login':
            self.open_page(PRODUCT_URL, '#exportSearchedGoods')
            return dict(ok=True)
        return ChromeExportWorker.call(self, op, timeout=timeout)
