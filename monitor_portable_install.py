"""Current-user Windows installation; no credentials or administrative actions."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

VERSION = '2.0.0'
_PRODUCT = 'DingTalkSourceMonitor'
_SHORTCUT_SCRIPT = '''Option Explicit
Dim shell, link
Set shell = CreateObject("WScript.Shell")
Set link = shell.CreateShortcut(WScript.Arguments(0))
link.TargetPath = WScript.Arguments(1)
link.Arguments = WScript.Arguments(2)
link.WorkingDirectory = WScript.Arguments(3)
link.IconLocation = WScript.Arguments(1) & ",0"
link.Description = "DingTalk Source Monitor"
link.Save
'''


def _local_root() -> Path:
    value = os.environ.get('LOCALAPPDATA')
    if not value:
        raise RuntimeError('找不到当前用户的本地应用目录（LOCALAPPDATA）。')
    return Path(value) / _PRODUCT


def default_data_dir() -> Path:
    return _local_root() / 'data'


def resource_path(name: str) -> Path:
    if Path(name).name != name:
        raise ValueError('资源名称不能包含目录。')
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / name
    root = Path(__file__).resolve().parent
    asset = root / 'monitor_assets' / name
    return asset if asset.is_file() else root / name


def bundled_dws() -> str:
    asset = resource_path('dws.exe')
    if asset.is_file():
        if getattr(sys, 'frozen', False):
            # Child CLI processes may outlive the GUI; never execute them from
            # PyInstaller's disposable extraction directory.
            import hashlib
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            runtime = _local_root() / 'runtime' / digest
            runtime.mkdir(parents=True, exist_ok=True)
            stable = runtime / 'dws.exe'
            if not stable.exists():
                temporary = runtime / ('dws-' + str(os.getpid()) + '.tmp')
                shutil.copyfile(asset, temporary)
                try:
                    # Another process may have published and started this exact
                    # binary after our exists check. Never replace a running EXE.
                    os.rename(temporary, stable)
                except OSError:
                    if not stable.is_file() or hashlib.sha256(stable.read_bytes()).hexdigest() != digest:
                        raise
            return str(stable)
        return str(asset)
    installed = shutil.which('dws.exe')
    if installed:
        return installed
    raise RuntimeError('未找到 DWS 程序，请使用包含 dws.exe 的完整安装包。')


def _hidden_flags() -> int:
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


def launch_worker(data_dir, executable=None) -> subprocess.Popen:
    program = str(executable or sys.executable)
    environment = None
    if getattr(sys, 'frozen', False):
        # The worker outlives its GUI and must own its onefile extraction directory.
        environment = os.environ.copy()
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        command = [program, '--worker', '--data-dir', str(data_dir)]
    else:
        if executable is None:
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            if pythonw.is_file():
                program = str(pythonw)
        command = [program, str(Path(__file__).resolve().with_name('monitor_tray_app.py')), '--worker', '--data-dir', str(data_dir)]
    try:
        return subprocess.Popen(command, creationflags=_hidden_flags(), shell=False, env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError(f'后台监控启动失败：{exc}') from exc


def _desktop_dir() -> Path:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            value, _ = winreg.QueryValueEx(key, 'Desktop')
            return Path(os.path.expandvars(value))
    except (ImportError, OSError) as exc:
        raise RuntimeError('无法读取当前用户的桌面位置，请检查 Windows 用户设置。') from exc


def _set_autostart(executable: Path, data_dir: Path) -> None:
    import winreg
    # Always quote the executable, including paths currently without spaces.
    command = '"' + str(executable) + '" ' + subprocess.list2cmdline(['--tray', '--data-dir', str(data_dir)])
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Run',
                            0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _PRODUCT, 0, winreg.REG_SZ, command)


def _create_shortcut(shortcut: Path, executable: Path, data_dir: Path) -> None:
    script = executable.parent / 'create_shortcut.vbs'
    script.write_text(_SHORTCUT_SCRIPT, encoding='ascii')
    cscript = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'cscript.exe'
    result = subprocess.run(
        [str(cscript), '//B', '//NoLogo', str(script), str(shortcut), str(executable),
         subprocess.list2cmdline(['--data-dir', str(data_dir)]), str(executable.parent)],
        shell=False, creationflags=_hidden_flags(), capture_output=True, timeout=30)
    if result.returncode or not shortcut.is_file():
        raise RuntimeError('创建桌面快捷方式失败；请检查 Windows 脚本宿主是否被禁用。')


def install_current_user(data_dir=None, source_executable=None, dry_run=False) -> dict:
    data = Path(data_dir).expanduser().resolve() if data_dir is not None else default_data_dir()
    target = _local_root() / f'app-{VERSION}' / '钉钉货源监控.exe'
    shortcut = _desktop_dir() / '钉钉货源监控.lnk'
    result = {'executable': str(target), 'data_dir': str(data),
              'shortcut': str(shortcut), 'autostart': False}
    if dry_run:
        return result
    if os.name != 'nt':
        raise RuntimeError('一键安装仅支持 Windows。')
    if source_executable is None and not getattr(sys, 'frozen', False):
        raise RuntimeError('请从打包后的监控 EXE 运行一键安装。')
    source = Path(source_executable or sys.executable).resolve()
    if not source.is_file() or source.suffix.lower() != '.exe':
        raise RuntimeError('找不到可安装的监控 EXE 文件。')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data.mkdir(parents=True, exist_ok=True)
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        if source != target.resolve():
            shutil.copy2(source, target)
        _create_shortcut(shortcut, target, data)
        _set_autostart(target, data)
        result['autostart'] = True
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'当前用户安装失败：{exc}') from exc
