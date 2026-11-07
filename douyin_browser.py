"""本工具专用的店铺浏览器：独立目录、本机 CDP、实例归属校验。

参考 Desktop-Tools-MCP 的专用 Chromium 实例设计；只保存端口和页面标识，
不读取或复制 Cookie、密码及日常浏览器配置。
"""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler
import json
import os
import re
import socket
import subprocess
import time
import uuid


def owns_port(port, process_id):
    """Windows TCP 监听表核对本次启动进程，避免首次欢迎页改写页面标记。"""
    if os.name != 'nt':
        return False
    import ctypes
    import struct
    from ctypes import wintypes
    class TcpRow(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ('state','local_address','local_port','remote_address','remote_port','pid')]
    api = ctypes.WinDLL('iphlpapi').GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD()
    api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    buffer = ctypes.create_string_buffer(size.value)
    if api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0) != 0:
        return False
    count = wintypes.DWORD.from_buffer(buffer).value
    for index in range(count):
        row = TcpRow.from_buffer(buffer, ctypes.sizeof(wintypes.DWORD) + index * ctypes.sizeof(TcpRow))
        if (row.pid == process_id and socket.ntohs(row.local_port & 0xffff) == port
                and socket.inet_ntoa(struct.pack('I',row.local_address)) == '127.0.0.1'):
            return True
    return False


def find_browser(kind):
    if kind not in ('chrome', 'edge'):
        raise ValueError('请选择 Chrome 或 Edge')
    relative = 'Google/Chrome/Application/chrome.exe' if kind == 'chrome' else 'Microsoft/Edge/Application/msedge.exe'
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        folder = os.environ.get(variable)
        if folder and (Path(folder) / relative).is_file():
            return Path(folder) / relative
    raise ValueError('未找到所选浏览器，请安装后重试')


def socket_url(value, port):
    """状态文件和浏览器返回值只能连接本机、当前端口的 DevTools 通道。"""
    parsed = urlparse(str(value))
    if (parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or parsed.port != port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r'/devtools/(browser|page)/[A-Za-z0-9-]+', parsed.path)):
        raise ValueError('浏览器连接归属无效，请重新打开本店浏览器')
    return str(value)


class CdpChannel:
    """有截止时间的 CDP 消息通道，事件与命令回包分开处理。"""
    def __init__(self, url, port, connector=None):
        if connector is None:
            from websockets.sync.client import connect
            connector = connect
        self.socket = connector(socket_url(url, port), open_timeout=5, close_timeout=1, proxy=None, max_size=4 * 1024 * 1024)
        self.sequence = 0
        self.events = deque()

    def command(self, method, params=None, timeout=15):
        self.sequence += 1
        self.socket.send(json.dumps(dict(id=self.sequence, method=method, params=params or {})))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = json.loads(self.socket.recv(timeout=max(.01, deadline - time.monotonic())))
            if value.get('id') == self.sequence:
                if 'error' in value:
                    raise ValueError('浏览器未完成操作，请重新连接本店窗口')
                return value.get('result', {})
            if value.get('method', '').startswith('Browser.download'):
                self.events.append(value)
        raise ValueError('浏览器操作超时，原库存已保留')

    def event(self, timeout):
        if self.events:
            return self.events.popleft()
        return json.loads(self.socket.recv(timeout=timeout))

    def close(self):
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ShopBrowser:
    def __init__(self, root, ident, kind, *, launcher=subprocess.Popen):
        if not re.fullmatch(r'[a-f0-9]{32}', ident) or kind not in ('chrome', 'edge'):
            raise ValueError('店铺浏览器配置无效')
        self.root = Path(root).resolve()
        self.ident, self.kind, self.launcher = ident, kind, launcher
        self.folder = self.root / 'managed-browsers' / ident / kind
        self.user_data = self.folder / 'user-data'
        self.state_file = self.folder / 'session.json'
        if self.folder.resolve() != self.folder or self.state_file.is_symlink():
            raise ValueError('本店浏览器目录不能链接到其他位置')
        self.state = {}
        try:
            self.state = json.loads(self.state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if not isinstance(self.state, dict):
            self.state = {}

    def read(self, port, path):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('本店浏览器端口无效')
        # 本机控制通道不走系统代理，不涉及站点接口或认证资料。
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/json/{path}', timeout=1) as response:
            return json.load(response)

    def running(self):
        if not self.state.get('browser_id'):
            return None
        try:
            version = self.read(self.state['port'], 'version')
        except (OSError, ValueError):
            return None
        url = socket_url(version.get('webSocketDebuggerUrl'), self.state['port'])
        if url.rsplit('/', 1)[-1] != self.state['browser_id']:
            raise ValueError('端口已被其他浏览器占用，未连接；请重新打开本店浏览器')
        return url

    def save(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.state_file)

    def open(self, url):
        """仅在用户主动打开登录窗口时启动；浏览器自行保存该店登录状态。"""
        if not self.running():
            executable = find_browser(self.kind)
            if not self.user_data.resolve().is_relative_to(self.root):
                raise ValueError('本店浏览器目录超出工具数据目录')
            self.user_data.mkdir(parents=True, exist_ok=True)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
            marker = 'about:blank#inventory-shop-' + uuid.uuid4().hex
            arguments = [str(executable), '--user-data-dir=' + str(self.user_data),
                         '--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
                         '--profile-directory=Default', '--no-first-run', '--no-default-browser-check', marker]
            # 这是用户点击“打开本店浏览器”需要使用的交互窗口。
            process = self.launcher(arguments, creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    version = self.read(port, 'version')
                    owned = [p for p in self.read(port, 'list') if p.get('type') == 'page' and p.get('url') == marker]
                    if len(owned) == 1 or owns_port(port, process.pid):
                        browser_url = socket_url(version.get('webSocketDebuggerUrl'), port)
                        if len(owned) == 1:
                            page_id = owned[0]['id']
                        else:
                            with CdpChannel(browser_url, port) as channel:
                                page_id = channel.command('Target.createTarget',dict(url='about:blank'))['targetId']
                        self.state = dict(port=port, browser_id=browser_url.rsplit('/', 1)[-1], page=page_id)
                        self.save()
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            else:
                raise ValueError('本店浏览器尚未连接；如已打开，请关闭本店专用窗口后再试')
        self.navigate(url)
        self.focus()

    def target(self):
        browser_url = self.running()
        if not browser_url:
            raise ValueError('本店浏览器未打开，请在登录设置点击“打开本店浏览器”')
        port = self.state['port']
        pages = self.read(port, 'list')
        target = next((p for p in pages if p.get('type') == 'page' and p.get('id') == self.state.get('page')), None)
        if target is None:
            # 只在已经验证归属的专用实例中新建页面，不接管用户当前标签。
            with CdpChannel(browser_url, port) as channel:
                self.state['page'] = channel.command('Target.createTarget', dict(url='about:blank'))['targetId']
            self.save()
            target = next((p for p in self.read(port, 'list') if p.get('id') == self.state['page']), None)
        if target is None:
            raise ValueError('未取得本店页面，请重新连接')
        return socket_url(target.get('webSocketDebuggerUrl'), port)

    @contextmanager
    def page(self):
        with CdpChannel(self.target(), self.state['port']) as channel:
            yield channel

    def evaluate(self, expression):
        with self.page() as channel:
            result = channel.command('Runtime.evaluate', dict(expression=expression, awaitPromise=True, returnByValue=True))
        if result.get('exceptionDetails'):
            raise ValueError('本店页面读取失败，请重新连接')
        return result.get('result', {}).get('value')

    def navigate(self, url):
        with self.page() as channel:
            result = channel.command('Page.navigate', dict(url=url))
        if result.get('errorText'):
            raise ValueError('页面未打开，请检查本店浏览器网络连接')

    def focus(self):
        with self.page() as channel:
            channel.command('Page.bringToFront')

    def browser_channel(self):
        url = self.running()
        if not url:
            raise ValueError('本店浏览器已关闭，请重新打开')
        return CdpChannel(url, self.state['port'])
