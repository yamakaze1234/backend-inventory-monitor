"""使用两份全新无登录目录验证真实 Chrome 页面崩溃恢复，不连接抖店。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import time

from douyin_browser import BrowserInterrupted, ShopBrowser


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<!doctype html><title>offline-recovery-check</title><p>isolated fixture</p>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    parent = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'real-browser-recovery'
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=parent))
    processes = []

    def launch(arguments, **_):
        # 无界面、隔离测试目录；不使用或关闭任何现用店铺进程。
        process = subprocess.Popen([*arguments[:-1], '--headless=new', '--disable-background-networking',
                                    arguments[-1]], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        processes.append(process)
        return process

    server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    browsers = [ShopBrowser(root, digit * 32, 'chrome', launcher=launch) for digit in ('a', 'b')]
    try:
        for index, browser in enumerate(browsers):
            browser.open(url + f'/shop{index}')
        first, second = browsers
        original_second = dict(second.state)
        original_pages = {p['id'] for p in first.read(first.state['port'], 'list') if p.get('type') == 'page'}
        with first.browser_channel() as monitor:
            with first.page() as channel:
                try:
                    channel.command('Page.crash', timeout=8)
                except BrowserInterrupted:
                    pass
                else:
                    raise AssertionError('真实页面崩溃没有产生明确异常')
            deadline = time.monotonic() + 10
            while True:
                assert time.monotonic() < deadline, '下载通道没有收到当前页面崩溃事件'
                try:
                    monitor.event(1)
                except TimeoutError:
                    continue
                except BrowserInterrupted:
                    break
        old_page = first.state['page']
        first.recover(url + '/recovered')
        assert first.state['page'] != old_page
        deadline = time.monotonic() + 15
        while not first.evaluate('document.readyState === "complete"'):
            assert time.monotonic() < deadline, '恢复后的测试页面未就绪'
            time.sleep(.2)
        assert first.evaluate('location.pathname') == '/recovered'
        assert second.state == original_second
        assert second.evaluate('location.pathname') == '/shop1'
        previous = first.evaluate('performance.timeOrigin')
        first.reload()
        deadline = time.monotonic() + 15
        while first.evaluate('performance.timeOrigin') == previous:
            assert time.monotonic() < deadline, '原页面刷新没有切换文档'
            time.sleep(.2)
        # Chrome 可能自带空白页；恢复只能替换巡检页，不能顺手关闭其他标签。
        remaining = {p['id'] for p in first.read(first.state['port'], 'list') if p.get('type') == 'page'}
        assert remaining == (original_pages - {old_page}) | {first.state['page']}
        print(json.dumps(dict(crash_detected=True, owned_page_recovered=True,
                              download_crash_detected=True, other_shop_unchanged=True,
                              reload_new_document=True)))
    finally:
        for browser in browsers:
            if browser.state:
                try:
                    browser.close_after_collection()
                except Exception:
                    pass
        # 兜底也只清理由本脚本创建并持有进程句柄的测试实例。
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
