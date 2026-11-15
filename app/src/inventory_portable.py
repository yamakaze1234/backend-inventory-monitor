"""一体迁移包的原生 EXE 启动支持，不依赖外部启动脚本。"""
import json
import os
from pathlib import Path
import shutil


def package_root(install_folder):
    """仅识别明确的一体包结构，不搜索用户的其他安装。"""
    folder = Path(install_folder).resolve()
    root = folder.parent.parent
    if (folder.parent.name == 'inventory' and
            (root / 'robot/runtime/pwsh/pwsh.exe').is_file() and
            (root / 'settings/inventory.sqlite3').is_file()):
        return root
    return None


def selected_workspace(install_folder):
    root = package_root(install_folder)
    if root is None:
        return None
    preferences = root / 'device-paths.json'
    if preferences.exists():
        try:
            value = json.loads(preferences.read_text(encoding='utf-8-sig'))
            selected = value.get('workspace')
            if selected:
                path = Path(selected)
                if not path.is_absolute() or not (path / 'inventory.sqlite3').is_file():
                    raise ValueError('原工作区不存在，请在迁移助手中重新选择，避免创建空工作区。')
                return path.resolve()
        except (OSError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise ValueError('一体包工作区配置无法读取，请检查 device-paths.json') from exc
    return root / 'workspace'


def configure_environment(install_folder):
    """随软件启动补充便携工具，子进程和更新重启会继承环境。"""
    root = package_root(install_folder)
    if root is None:
        return
    runtime = root / 'robot/runtime'
    paths = [str(runtime / name) for name in ('pwsh', 'node', 'dws')]
    current = os.environ.get('PATH', '').split(os.pathsep)
    os.environ['PATH'] = os.pathsep.join(paths + [p for p in current if p not in paths])


def initialize_workspace(install_folder, workspace):
    """仅为一体包默认空工作区导入设置，已存在数据库一律保留。"""
    root = package_root(install_folder)
    if root is None or Path(workspace).resolve() != (root / 'workspace').resolve():
        return
    target = Path(workspace) / 'inventory.sqlite3'
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建防止并发启动覆盖已有配置。
    try:
        with target.open('xb') as output, (root / 'settings/inventory.sqlite3').open('rb') as source:
            shutil.copyfileobj(source, output)
    except FileExistsError:
        return

