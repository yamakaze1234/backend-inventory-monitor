"""库存工作区隔离：显式目录优先，旧安装原位保留，新安装不接管全局业务数据。"""
from pathlib import Path
import hashlib
import os


def resolve_data_root(requested, install_folder, *, local_app_data=None):
    """不创建目录、不读取凭据；复制程序到新目录会得到独立的默认工作区。"""
    if requested:
        return Path(requested).expanduser().resolve()
    folder = Path(install_folder).resolve()
    # 一体包的迁移助手已选定工作区时，以它为准，避免旁边的历史数据库截走配置。
    from inventory_portable import selected_workspace
    portable = selected_workspace(folder)
    if portable is not None:
        return portable
    legacy = folder / '.local_inventory_ui_test'
    if (legacy / 'inventory.sqlite3').is_file():
        return legacy
    local = local_app_data or os.environ.get('LOCALAPPDATA')
    if not local:
        raise ValueError('找不到当前用户数据目录，请使用 --data-dir 指定工作区')
    identity = hashlib.sha256(os.path.normcase(str(folder)).encode('utf-8')).hexdigest()[:16]
    return Path(local).resolve() / 'BackendInventoryMonitor' / 'workspaces' / identity


def message_data_root(service, *, legacy=False):
    """只沿用本工作区已有绑定；新工作区不会发现其他部门的全局机器人数据。"""
    with service.db() as db:
        saved = service.get(db, 'daily_source_root', '')
        if saved:
            return Path(saved).expanduser().resolve()
        if legacy:
            from monitor_portable_install import default_data_dir
            selected = default_data_dir().resolve()
        else:
            selected = service.root.resolve()
        service.put(db, 'daily_source_root', str(selected))
    return selected


def workspace_settings(service):
    """只返回工作区名称和位置，不包含SQL、机器人或登录资料。"""
    with service.db() as db:
        department = service.get(db, 'department_name', '')
        name = service.get(db, 'workspace_name', '')
    return dict(department=department, name=name, data_dir=str(service.root.resolve()))


def save_workspace_settings(service, values):
    """部门名称用于区分工作区，不隐式切换目录或搬迁现有数据。"""
    clean = {key: str(values.get(key, '')).strip() for key in ('department', 'name')}
    if any(len(value) > 80 or any(ord(c) < 32 for c in value) for value in clean.values()):
        raise ValueError('部门和工作区名称最多 80 字，不能包含控制字符')
    with service.db() as db:
        service.put(db, 'department_name', clean['department'])
        service.put(db, 'workspace_name', clean['name'])
    return workspace_settings(service)
