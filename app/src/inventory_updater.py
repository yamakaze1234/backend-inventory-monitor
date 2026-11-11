"""签名更新通道：固定发布源、大小及摘要校验；业务数据不参与替换。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from inventory_version import VERSION
from project_paths import UI_ROOT

SOURCES = (
    'https://gitea.cq360.top/CoreStudio/backend-inventory-monitor/releases/download/inventory-updates/inventory-update.json',
    'https://github.com/yamakaze1234/backend-inventory-monitor/releases/download/inventory-updates/inventory-update.json',
)
PREFIXES = ('https://gitea.cq360.top/CoreStudio/backend-inventory-monitor/releases/download/',
            'https://github.com/yamakaze1234/backend-inventory-monitor/releases/download/')
MAX_BINARY = 512 * 1024 * 1024


def powershell_executable():
    """安装后无需重新登录；优先 PATH，其次使用 PowerShell 7 标准安装位置。"""
    found = shutil.which('pwsh.exe')
    if found:
        return found
    for variable in ('ProgramFiles', 'LOCALAPPDATA'):
        root = os.environ.get(variable)
        if root:
            suffix = 'PowerShell/7/pwsh.exe' if variable == 'ProgramFiles' else 'Microsoft/PowerShell/7/pwsh.exe'
            candidate = Path(root) / suffix
            if candidate.is_file():
                return str(candidate)
    raise ValueError('缺少 PowerShell 7，请先运行安装包中的“一键部署环境.cmd”')


def restart_arguments(arguments):
    """仅保留工作区和显示方式，不透传旧健康回执、worker 或任意命令。"""
    result=[]
    iterator=iter(arguments)
    for argument in iterator:
        if argument in ('--tray','--legacy-ui'):
            result.append(argument)
        elif argument == '--data-dir':
            value=next(iterator,None)
            if value:
                result.extend(('--data-dir',str(Path(value).resolve())))
        elif argument.startswith('--data-dir='):
            value=argument.partition('=')[2]
            if value:
                result.extend(('--data-dir',str(Path(value).resolve())))
    return result


def version_key(value):
    """兼容现有 1.06 格式，正式通道不接受预发布版本或不完整版本。"""
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', value):
        raise ValueError('更新版本格式无效')
    parts = tuple(map(int, value.split('.')))
    return parts + (0,) * (3-len(parts))


def verify_manifest(envelope, public_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        payload = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(signature, payload)
        manifest = json.loads(payload)
    except Exception:
        raise ValueError('更新清单签名无效') from None
    if not isinstance(manifest, dict) or manifest.get('app') != 'backend-inventory-monitor' or manifest.get('platform') != 'windows-x86_64':
        raise ValueError('更新包与当前程序不匹配')
    version_key(manifest.get('version'))
    if type(manifest.get('size')) is not int or not 0 < manifest['size'] <= MAX_BINARY:
        raise ValueError('更新包大小无效')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', str(manifest.get('sha256', ''))):
        raise ValueError('更新摘要无效')
    urls = manifest.get('urls', [])
    if not isinstance(urls, list) or not urls or any(not isinstance(u,str) or not u.startswith(PREFIXES) or urlparse(u).username for u in urls):
        raise ValueError('更新下载地址不在发布源内')
    return manifest


class InventoryUpdater:
    def __init__(self, root, public_key=None, opener=None):
        self.root = Path(root) / 'updates'
        self.public_key = public_key
        self.opener = opener or urllib.request.urlopen
        self.lock = threading.Lock()
        self.release = None
        self.staged = None
        self.state = dict(phase='idle', currentVersion=VERSION, available=False, received=0, total=0, error='')

    def snapshot(self):
        result=dict(self.state)
        receipt=self.root/'install.json.result'
        if receipt.is_file():
            result['lastResult']=receipt.read_text(encoding='utf-8-sig')[:1000]
        return result

    def _open(self, url):
        response = self.opener(urllib.request.Request(url, headers={'User-Agent':'InventoryMonitor-Updater'}), timeout=30)
        # GitHub 附件会重定向到 CDN，但始终必须使用 HTTPS。
        if hasattr(response, 'geturl') and urlparse(response.geturl()).scheme != 'https':
            response.close()
            raise ValueError('更新源重定向无效')
        return response

    def check(self):
        if not self.lock.acquire(False):
            raise ValueError('更新任务正在进行')
        try:
            self.release = self.staged = None
            # 重新检查时清空上一轮下载展示，避免把旧版本进度当成本轮结果。
            self.state.update(phase='checking', error='', available=False, received=0, total=0, notes='', version='')
            key = self.public_key or (UI_ROOT / 'update-public-key.txt').read_text(encoding='ascii').strip()
            for url in SOURCES:
                try:
                    with self._open(url) as response:
                        raw = response.read(128*1024+1)
                    if len(raw) > 128*1024:
                        raise ValueError('更新清单过大')
                    release = verify_manifest(json.loads(raw), key)
                    self.release = release
                    self.state.update(phase='ready', available=version_key(release['version']) > version_key(VERSION),
                                      version=release['version'], notes=release.get('notes',''), source=url, total=release['size'])
                    return self.snapshot()
                except Exception:
                    continue
            raise ValueError('两个更新源均未提供有效的签名清单，请稍后重试')
        except Exception as error:
            self.state.update(phase='error', error=str(error))
            raise
        finally:
            self.lock.release()

    def download(self):
        if not self.lock.acquire(False):
            raise ValueError('更新任务正在进行')
        try:
            if not self.release or not self.state['available']:
                raise ValueError('请先检查可用更新')
            self.staged = None
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / (self.release['sha256'] + '.exe')
            self.state.update(phase='downloading', received=0, error='')
            for url in self.release['urls']:
                try:
                    digest, received = hashlib.sha256(), 0
                    with self._open(url) as response, target.open('wb') as output:
                        while chunk := response.read(1024*1024):
                            received += len(chunk)
                            if received > self.release['size']:
                                raise ValueError('更新包超出声明大小')
                            digest.update(chunk)
                            output.write(chunk)
                            self.state['received'] = received
                    if received != self.release['size'] or digest.hexdigest() != self.release['sha256'].lower():
                        raise ValueError('更新包校验失败')
                    with target.open('rb') as stream:
                        if stream.read(2) != b'MZ':
                            raise ValueError('更新包不是 Windows 程序')
                    self.staged = target
                    self.state.update(phase='downloaded')
                    return self.snapshot()
                except Exception:
                    continue
            raise ValueError('更新包下载或校验失败，旧程序未改变')
        except Exception as error:
            self.state.update(phase='error', error=str(error))
            raise
        finally:
            self.lock.release()

    def install(self, busy=False):
        """调用方须先安全退出采集及消息线程；辅助进程等父进程退出后替换。"""
        if busy:
            raise ValueError('存在排队、执行中或待核验的匹配任务，请完成后更新')
        if not getattr(sys, 'frozen', False):
            raise ValueError('源码运行不替换 Python，请在正式安装版中更新')
        shell = powershell_executable()
        if self.state['phase'] not in ('downloaded','preparing') or not self.staged or not self.release:
            raise ValueError('尚未准备好更新包')
        if hashlib.sha256(self.staged.read_bytes()).hexdigest() != self.release['sha256'].lower():
            raise ValueError('暂存更新包已改变，请重新下载')
        plan = self.root / 'install.json'
        ready = self.root / ('helper-ready-' + uuid.uuid4().hex + '.txt')
        target = Path(sys.executable).resolve()
        # 安装计划仅包含本进程确定的路径，不接受网页传入可执行路径。
        plan.write_text(json.dumps(dict(pid=os.getpid(), target=str(target), source=str(self.staged.resolve()),
            sha256=self.release['sha256'], marker=str((self.root/('healthy-'+uuid.uuid4().hex+'.txt')).resolve()),
            expected=self.release['version'],ready=str(ready.resolve()),arguments=restart_arguments(sys.argv[1:])), ensure_ascii=False), encoding='utf-8')
        helper = self.root / 'apply-update.ps1'
        helper.write_bytes((UI_ROOT / 'apply-update.ps1').read_bytes())
        process = subprocess.Popen([shell,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(helper),'-Plan',str(plan)],
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        # 助手先确认能读计划并等待父进程，再允许主程序退出；启动失败仍保留旧程序。
        deadline = time.monotonic() + 15
        while not ready.is_file():
            if process.poll() is not None or time.monotonic() >= deadline:
                if process.poll() is None:
                    # 父进程仍在运行，助手尚不能替换文件；取消本次助手，防止以后退出时误安装。
                    process.terminate()
                    process.wait(timeout=5)
                raise ValueError('更新助手未就绪，当前程序继续运行，请检查 PowerShell 环境')
            time.sleep(.1)
        self.state['phase'] = 'installing'
        return dict(message='正在安全退出并安装更新')
