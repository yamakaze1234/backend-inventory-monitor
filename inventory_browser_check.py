"""专用浏览器离线验收：仅访问本机样例页面，使用独立空目录和无界面窗口。"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import time
import uuid

from douyin_browser import ShopBrowser, find_browser, CdpChannel, owns_port
from douyin_managed_export import ManagedExportWorker


def run(output_directory):
    from openpyxl import Workbook
    output = Path(output_directory).resolve(); output.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix='offline-browser-', dir=output))
    book = Workbook(); book.active.append(['离线下载验收']); buffer = BytesIO(); book.save(buffer); book.close()
    workbook = buffer.getvalue()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            self.send_response(200)
            if self.path == '/download':
                self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                self.send_header('Content-Disposition','attachment; filename="productExport-offline.xlsx"')
                content = workbook
            else:
                self.send_header('Content-Type','text/html; charset=utf-8')
                name = self.path.strip('/')
                content = ('<!doctype html><html><body><div id="shop">'+name+'</div><a href="/download">下载报表</a></body></html>').encode()
            self.send_header('Content-Length', str(len(content)));self.end_headers();self.wfile.write(content)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    launched = []
    def launch(arguments, **kwargs):
        # 验收窗口没有真实账号，隐藏启动且关闭首次运行后台功能。
        startup = subprocess.STARTUPINFO();startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen([arguments[0], '--headless=new', '--disable-gpu', '--disable-background-networking', *arguments[1:]],
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,startupinfo=startup,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        port = int(next(arg.split('=',1)[1] for arg in arguments if arg.startswith('--remote-debugging-port=')))
        launched.append((process,port))
        return process
    kinds = []
    for kind in ('chrome','edge'):
        try:
            find_browser(kind);kinds.append(kind)
        except ValueError:
            pass
    if not kinds:
        raise ValueError('离线浏览器验收需要已安装 Chrome 或 Edge')
    browsers = [ShopBrowser(data,uuid.uuid4().hex,kinds[i % len(kinds)],launcher=launch) for i in range(3)]
    results = []
    try:
        def check(index):
            browser = browsers[index]
            name = 'offline-shop-' + str(index)
            browser.open(f'http://127.0.0.1:{server.server_port}/'+name)
            worker = ManagedExportWorker(None,data/'profiles'/browser.ident)
            worker._browser = browser
            worker.wait('document.querySelector("#shop")?.textContent === '+json.dumps(name))
            def check_shop():
                if browser.evaluate('document.querySelector("#shop")?.textContent') != name:
                    raise ValueError('本店页面身份不匹配')
            worker.check_shop = check_shop
            worker.click('a','下载报表')
            download = worker.wait_download(20)
            assert Path(download['filename']).read_bytes() == workbook
            # 重新创建控制器仍能找到本店窗口，证明连接状态可跨程序实例恢复。
            reopened = ShopBrowser(data,browser.ident,browser.kind)
            assert reopened.evaluate('document.querySelector("#shop")?.textContent') == name
            return dict(shop=name,browser=browser.kind,port=browser.state['port'],download_verified=True,reconnected=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(check,range(3)))
        assert len({r['port'] for r in results}) == 3
        assert len({b.user_data for b in browsers}) == 3
    finally:
        for browser in browsers:
            try:
                with browser.browser_channel() as channel:
                    channel.command('Browser.close',timeout=3)
            except Exception:
                pass
        for process, port in launched:
            # 首次启动验证失败时尚未写入状态，只用本次持有的进程和端口收尾。
            if owns_port(port, process.pid):
                try:
                    version = browsers[0].read(port,'version')
                    with CdpChannel(version['webSocketDebuggerUrl'],port) as channel:
                        channel.command('Browser.close',timeout=3)
                except Exception:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise RuntimeError('离线验收浏览器尚未退出，测试目录保留，未强制终止') from None
        server.shutdown();server.server_close()
    evidence = dict(three_isolated_browsers=True,results=results,real_shops_accessed=0,notifications_sent=0)
    (output/'browser-result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser();parser.add_argument('--output',required=True)
    run(parser.parse_args().output)
