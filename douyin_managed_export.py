"""独立店铺浏览器的官方导出适配：复用报表校验，仅替换页面连接方式。"""
from pathlib import Path
import json
import re
import threading
import time

from douyin_browser import ShopBrowser
from douyin_export import ChromeMachineExportWorker, PRODUCT_URL, SHOP_SELECTOR


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

    def browser(self):
        if not hasattr(self, '_browser'):
            self._browser = ShopBrowser(self.root, self.ident, self.expected_browser)
        return self._browser

    def close(self):
        # 只停止本工具操作，不退出用户已登录的专用浏览器。
        self.stopped.set()

    def eval(self, expression):
        if self.stopped.is_set():
            raise ValueError('本店检查已停止')
        return self.browser().evaluate(expression)

    def wait(self, expression, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.eval(expression):
                return
            self.pause(.2)
        raise ValueError('页面未就绪或登录失效，请在本店浏览器完成登录后重新连接')

    def open_page(self, url, ready):
        self.browser().navigate(url)
        expected = json.dumps(url.split('?', 1)[0])
        # 商品控件会先于顶部店名出现；店名未加载不能当作账号失效或店铺不符。
        # 这里只等待非空店名，随后仍由 check_shop 严格拒绝其他店铺。
        self.wait(f'location.href.split("?")[0] === {expected} && document.readyState !== "loading"'
                  f' && !!document.querySelector({json.dumps(ready)})'
                  f' && !!document.querySelector({json.dumps(SHOP_SELECTOR)})?.textContent?.trim()')
        self.check_shop()

    def click(self, selector, text):
        self.check_shop()
        if text == '下载报表':
            self.prepare_download()
        expression = '''(() => {const nodes = [...document.querySelectorAll(SELECTOR)].filter(e =>
          e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && e.textContent.trim() === TEXT);
          if(nodes.length !== 1 || nodes[0].disabled) return false;
          nodes[0].click(); return true;})()'''.replace('SELECTOR', json.dumps(selector)).replace('TEXT', json.dumps(text))
        if self.eval(expression) is not True:
            raise ValueError('未找到唯一可操作的“' + text + '”，本轮未采用')

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
            self.browser().open(PRODUCT_URL)
        if op == 'login':
            self.open_page(PRODUCT_URL, '#exportSearchedGoods')
            return dict(ok=True)
        try:
            return super().call(op, timeout=timeout)
        finally:
            if self.download:
                self.download.close()
                self.download = None
