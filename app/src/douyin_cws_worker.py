"""工作台到独立 CWS 运行时的边界；保留原调度、快照与终态提醒。"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import threading
import time

SUPPORT = ('douyin_cws_export', 'douyin_export', 'douyin_managed_export',
           'douyin_browser', 'douyin_inventory', 'douyin_scope')
_SLOTS = threading.BoundedSemaphore(2)


def binding(workspace, ident):
    """设备配置保存在工作区，升级程序时不重写。"""
    path = Path(workspace) / 'cws.json'
    try:
        stores = json.loads(path.read_text(encoding='utf-8'))['stores']
        value = stores[ident]
        for key in ('executable', 'workspace', 'data_dir'):
            if not Path(value[key]).is_absolute():
                raise ValueError()
        if not Path(value['executable']).is_file() or not value['profile'].strip():
            raise ValueError()
        if type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
            raise ValueError()
        # 每店独立浏览器实例；端口和目录冲突不能借用另一店的登录。
        for key, other in stores.items():
            if key != ident and (other['port'] == value['port'] or
                    any(Path(other[k]).resolve() == Path(value[k]).resolve() for k in ('workspace', 'data_dir'))):
                raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('CWS 本店连接配置不完整，请检查工作区 cws.json') from None


def install_scripts(workspace):
    """只部署随程序提供的公开脚本，不复制配置、登录或业务数据。"""
    frozen = getattr(sys, '_MEIPASS', None)
    support = Path(frozen) / 'cws-support' if frozen else Path(__file__).parent
    scripts = Path(frozen) / 'cws' if frozen else Path(__file__).resolve().parents[2] / 'integrations/cws'
    target = Path(workspace) / 'config/plugins/scraper'
    (target / 'support').mkdir(parents=True, exist_ok=True)
    (target / 'scripts/DY/库存').mkdir(parents=True, exist_ok=True)
    for name in SUPPORT:
        shutil.copy2(support / (name + '.py'), target / 'support' / (name + '.py'))
    shutil.copy2(scripts / 'DY-官方库存巡检.py', target / 'scripts/DY/库存/DY-官方库存巡检.py')


class CwsWorker:
    def __init__(self, tool_root, profile):
        self.ident = Path(profile).name
        self.root = Path(profile).resolve().parent.parent
        self.workspace = self.root.parent
        self.expected_shop = ''
        self.stopped = threading.Event()

    def close(self):
        self.stopped.set()

    def _run(self, command, cwd, timeout):
        # 清理代理环境；打包进程不向外部 Python 注入自身 DLL 路径。
        env = {k: v for k, v in os.environ.items() if k.lower() not in
               ('http_proxy', 'https_proxy', 'all_proxy', 'pythonpath', 'pythonhome')}
        env['NO_PROXY'] = '*'
        from douyin_erp import _SPAWN_LOCK
        bundle = getattr(sys, '_MEIPASS', None)
        if bundle:
            env['PATH'] = os.pathsep.join(p for p in env.get('PATH', '').split(os.pathsep)
                                         if not os.path.abspath(p).casefold().startswith(str(bundle).casefold()))
        with _SPAWN_LOCK:
            if bundle and os.name == 'nt':
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
            try:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            finally:
                if bundle and os.name == 'nt':
                    ctypes.windll.kernel32.SetDllDirectoryW(str(bundle))
        deadline = time.monotonic() + timeout
        try:
            while not self.stopped.is_set() and time.monotonic() < deadline:
                try:
                    stdout, _ = process.communicate(timeout=1)
                    return process.returncode, stdout
                except subprocess.TimeoutExpired:
                    continue
            raise ValueError('CWS 巡检已停止' if self.stopped.is_set() else 'CWS 巡检超时，保留原报表任务')
        finally:
            if process.poll() is None:
                # 仅结束本次命令树，不关闭浏览器或其他业务服务；下次沿用待取报表。
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
                process.communicate()

    def call(self, operation, timeout=900):
        # 三店由原调度排队，最多同时使用两个独立 CWS 浏览器。
        while not self.stopped.is_set():
            if _SLOTS.acquire(timeout=.5):
                try:
                    return self._call(operation, timeout)
                finally:
                    _SLOTS.release()
        raise ValueError('CWS 巡检已停止')

    def _call(self, operation, timeout):
        if operation not in ('open', 'login', 'collect'):
            raise ValueError('未知 CWS 操作')
        cfg = binding(self.workspace, self.ident)
        install_scripts(cfg['workspace'])
        common = ['--directory', cfg['workspace'], 'plugin', 'scraper']
        target = ['--name', cfg['profile'], '--data-dir', cfg['data_dir'], '--port', str(cfg['port'])]
        if operation == 'open':
            args = ['profile', 'open', *target]
        else:
            args = ['run', 'DY-官方库存巡检', *target, '--platform', 'DY', '--jobs', '1',
                    '--options', json.dumps({'店铺': self.expected_shop, '状态目录': str(self.root / 'cws-state' / self.ident),
                                           '操作': operation}, ensure_ascii=False)]
        code, stdout = self._run([cfg['executable'], *common, *args], cfg['workspace'], timeout)
        try:
            receipt = json.loads(stdout)
            if code or receipt.get('failed') or receipt.get('error'):
                raise ValueError()
            if operation == 'open':
                return {'ok': True}
            file = Path(receipt['file']).resolve()
            if not file.is_relative_to(Path(cfg['workspace']).resolve() / 'out'):
                raise ValueError()
            rows = json.loads(file.read_text(encoding='utf-8'))['records']
            if len(rows) != 1 or rows[0]['店铺'] != self.expected_shop:
                raise ValueError()
            result = json.loads(rows[0]['结果JSON'])
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, KeyError, TypeError, OSError):
            raise ValueError('CWS 采集未确认；请检查本店登录、店铺身份或 CWS 失败记录，未采用本轮数据') from None
        if result.get('pending') is True:
            from douyin_export import ReportPending
            raise ReportPending('官方报表生成中，沿用本店任务继续等待')
        if result.get('ok') is not True or (operation == 'collect' and result.get('complete') is not True):
            raise ValueError('CWS 官方报表未完整返回，本轮未采用')
        return result
