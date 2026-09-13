"""Small, credential-free bridge between the tray application and its worker."""
import ctypes
from ctypes import wintypes
import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import time

TASK_NAME = 'Codex-DingTalk-货源监控'
WORKER_UPGRADE_ERROR = '旧版钉钉后台未能安全退出，请退出旧版程序或重启电脑后再打开新版。'


def terminate_paused_worker(pid, paused_check, *, wait_ms=3000):
    """Verify and terminate the same process handle, only after a fresh pause ack."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x101001, False, int(pid))
    if not handle:
        raise RuntimeError(WORKER_UPGRADE_ERROR)
    try:
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            raise RuntimeError(WORKER_UPGRADE_ERROR)
        name = ntpath.basename(image.value).lower()
        if not (name.endswith('.exe') and name.startswith(('后台库存监控', '钉钉货源监控'))):
            raise RuntimeError(WORKER_UPGRADE_ERROR)
        if not paused_check():
            raise RuntimeError(WORKER_UPGRADE_ERROR)
        if not kernel.TerminateProcess(handle, 0):
            raise RuntimeError(WORKER_UPGRADE_ERROR)
        if kernel.WaitForSingleObject(handle, wait_ms) != 0:
            raise RuntimeError(WORKER_UPGRADE_ERROR)
    finally:
        kernel.CloseHandle(handle)


def app_settings():
    if getattr(sys, 'frozen', False):
        config = Path(sys.executable).parent / 'monitor_app.json'
        if config.is_file():
            return json.loads(config.read_text(encoding='utf-8-sig'))
        from monitor_portable_install import default_data_dir
        return {'workspace': str(default_data_dir()), 'portable': True}
    return {'workspace': str(Path(__file__).resolve().parent), 'powershell': 'pwsh.exe'}


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def is_process_alive(pid):
    if not pid:
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def summarize_status(snapshot, *, now=None, alive=None):
    alive = is_process_alive if alive is None else alive
    now = time.time() if now is None else now
    fresh = 0 <= now - snapshot.get('heartbeat', 0) < 25
    supervisor_alive = bool(snapshot.get('pid') and alive(snapshot.get('pid')))
    online = fresh and supervisor_alive
    sources = []
    for source in snapshot.get('sources', []):
        item = dict(source)
        item['live'] = bool(source.get('pid') and alive(source.get('pid')))
        item['ready'] = bool(online and item['live'] and source.get('status') == 'ready')
        sources.append(item)
    ready = sum(s['ready'] for s in sources)
    if not online:
        state = 'offline'
    elif snapshot.get('configured') is False and not sources:
        state = 'unconfigured'
    elif not snapshot.get('enabled', True):
        state = 'stopping' if any(s['live'] for s in sources) else 'paused'
    elif sources and ready == len(sources):
        state = 'running'
    elif not snapshot.get('activated', True):
        state = 'waiting'
    else:
        state = 'connecting'
    return {'state': state, 'online': bool(online), 'supervisor_alive': supervisor_alive,
            'ready': ready, 'sources': sources,
            'checked_at': snapshot.get('checked_at', '—')}


class Controller:
    def __init__(self, settings=None):
        self.settings = settings or app_settings()
        self.root = Path(self.settings['workspace'])
        self.runtime = self.root / 'monitor_runtime'

    def status(self):
        result = summarize_status(read_json(self.runtime / 'status.json', {}))
        if result['state'] == 'unconfigured' and not read_json(self.runtime / 'control.json', {}).get('enabled', True):
            result['state'] = 'paused'
        if not result['online'] and not read_json(self.runtime / 'control.json', {}).get('enabled', True):
            result['state'] = ('stopping' if result['supervisor_alive'] or
                               any(s['live'] for s in result['sources']) else 'paused')
        return result

    def set_enabled(self, enabled):
        requested_at = time.time()
        write_json(self.runtime / 'control.json', {
            'enabled': enabled, 'force_until': requested_at + 60 if enabled else 0,
            'requested_at': requested_at})
        return requested_at

    def ensure_task(self):
        if self.settings.get('portable'):
            from monitor_portable_install import launch_worker
            installed = read_json(self.root / 'installation.json', {})
            executable = self.settings.get('executable') or installed.get('executable')
            if executable and not Path(executable).is_file():
                executable = None
            launch_worker(self.root, executable=executable)
            return
        result = subprocess.run([self.settings['powershell'], '-NoLogo', '-NoProfile',
            '-NonInteractive', '-Command',
            "Start-ScheduledTask -TaskName 'Codex-DingTalk-货源监控' -ErrorAction Stop"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
        if result.returncode:
            raise RuntimeError('后台任务启动失败，请检查 Windows 计划任务。')

    def ensure_current_worker(self):
        """Replace a legacy portable worker only after it acknowledges a full pause."""
        snapshot = read_json(self.runtime / 'status.json', {})
        status = summarize_status(snapshot)
        if not status['supervisor_alive']:
            self.ensure_task()
            return
        if status['online'] and snapshot.get('robot_protocol', 0) >= 1:
            return
        if not self.settings.get('portable'):
            raise RuntimeError(WORKER_UPGRADE_ERROR)
        pid = snapshot['pid']
        requested_at = self.stop()
        deadline = time.monotonic() + 11

        def acknowledged():
            current = read_json(self.runtime / 'status.json', {})
            state = summarize_status(current)
            return (current.get('pid') == pid and current.get('heartbeat', 0) > requested_at
                    and current.get('enabled') is False and state['online']
                    and not any(source['live'] for source in state['sources']))

        while time.monotonic() < deadline:
            if acknowledged():
                try:
                    terminate_paused_worker(pid, acknowledged)
                except Exception as error:
                    raise RuntimeError(WORKER_UPGRADE_ERROR) from error
                self.ensure_task()
                return
            time.sleep(.2)
        raise RuntimeError(WORKER_UPGRADE_ERROR)

    def start(self):
        self.ensure_current_worker()
        self.set_enabled(True)

    def stop(self):
        return self.set_enabled(False)
