"""CWS 专店浏览器正常退出；先核对进程目录和端口，不清理登录资料。"""
from pathlib import Path
import ctypes
import json
import subprocess
import time
from urllib.request import build_opener, ProxyHandler
from douyin_browser import CdpChannel, owns_port


def arguments(command):
    """由 Windows 解析原始命令行，避免中文路径、空格和引号误匹配。"""
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    result = parse(command, ctypes.byref(count))
    if not result:
        raise ValueError('无法核对 CWS 浏览器归属')
    try:
        return [result[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(result)


def identity(cfg):
    """端口必须归属于使用本店独立数据目录的 Chromium 主进程。"""
    script = "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('msedge.exe','chrome.exe') } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                             '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;'+script],
                            capture_output=True, text=True, encoding='utf-8', timeout=15,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('无法核对 CWS 浏览器进程')
    rows = json.loads(result.stdout or '[]')
    for row in rows if isinstance(rows, list) else [rows]:
        if not owns_port(cfg['port'], row['ProcessId']):
            continue
        args = arguments(row.get('CommandLine') or '')
        values = dict(arg.split('=', 1) for arg in args if arg.startswith('--') and '=' in arg)
        if (Path(values.get('--user-data-dir', '')).resolve() != Path(cfg['data_dir']).resolve()
                or values.get('--remote-debugging-port') != str(cfg['port'])):
            raise ValueError('CWS 浏览器归属不一致，保留窗口')
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{cfg['port']}/json/version", timeout=3) as response:
            url = json.load(response)['webSocketDebuggerUrl']
        return row['ProcessId'], url
    return None


def close_browser(cfg):
    """通过 Browser.close 正常退出，关闭结果以监听端口释放为准。"""
    found = identity(cfg)
    if found is None:
        return
    pid, url = found
    try:
        with CdpChannel(url, cfg['port']) as channel:
            # 连接期间再次核对端口归属，不能关闭被其他进程复用的端口。
            if not owns_port(cfg['port'], pid):
                raise ValueError('CWS 浏览器归属发生变化')
            channel.command('Browser.close', timeout=5)
    except Exception:
        # Chromium 可能在回复之前断开；最终以实际监听状态确认。
        pass
    deadline = time.monotonic() + 8
    while owns_port(cfg['port'], pid):
        if time.monotonic() >= deadline:
            raise ValueError('本店 CWS 浏览器未确认退出，请手动关闭')
        time.sleep(.1)
