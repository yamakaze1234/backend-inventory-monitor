"""修复既有一体迁移包：保留配置、原 EXE 文件名与业务数据，不启动监控。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/src'))
import order_bot_config
from inventory_portable import selected_workspace


def inspect_package(root):
    """只接受已部署的一体包，不能把默认空工作区误当成原配置。"""
    root = Path(root).resolve()
    candidates = list((root / 'inventory').glob('*/*.exe'))
    candidates = [path for path in candidates if path.name.startswith(('后台库存监控_', 'InventoryMonitor-'))]
    if len(candidates) != 1:
        raise ValueError('无法唯一识别原库存程序，请选择原一体迁移包根目录（包含 inventory、robot、settings）。')
    target = candidates[0]
    workspace = selected_workspace(target.parent)
    if workspace is None or not (workspace / 'inventory.sqlite3').is_file():
        raise ValueError('原工作区不存在，未创建空配置；请先核对 device-paths.json。')
    config = order_bot_config.load(workspace)
    if not config:
        raise ValueError('原工作区没有订单配置，未覆盖任何文件；请确认选中了现用的一体包。')
    order_bot_config.normalize(config)
    order_bot_config.assert_state_owner(workspace, config['state_root'])
    order_bot_config.assert_stopped(config['state_root'])
    order_bot_config.assert_connector_stopped(config)
    # 仅检查目标安装和后端的进程，不结束其他安装或后台业务。
    command = 'Get-CimInstance Win32_Process | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress'
    result = subprocess.run([config['pwsh_path'], '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, encoding='utf-8', timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('无法检查运行进程，未替换程序。')
    rows = json.loads(result.stdout or '[]')
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        executable = (row.get('ExecutablePath') or '').casefold()
        commandline = (row.get('CommandLine') or '').casefold()
        if executable == str(target).casefold() or any(
                str(Path(config[key])).casefold() in commandline for key in ('match_root', 'control_root')):
            raise ValueError('原程序或订单后端仍在运行，请从托盘退出程序并停止订单服务后重新修复。')
    return target, workspace


def repair(root, check_only=False):
    # 校验全部载荷后再部署，避免拷贝中断或文件损坏时更新一半。
    manifest = json.loads((ROOT / 'repair-manifest.json').read_text(encoding='utf-8-sig'))
    for relative, expected in manifest['files'].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('修复包文件校验失败，请重新下载完整修复包。')
    target, workspace = inspect_package(root)
    if check_only:
        return dict(checked=True, target=str(target), workspace=str(workspace))
    backup = target.with_name(target.name + '.before-' + time.strftime('%Y%m%d-%H%M%S'))
    if backup.exists():
        raise ValueError('备份文件已存在，请稍后重试。')
    shutil.copy2(target, backup)
    try:
        # 复用正式部署的归属、停机、备份和回滚校验；不改账号和巡检开关。
        order_bot_config.deploy(workspace)
        shutil.copy2(ROOT / 'InventoryMonitor-v1.11.exe', target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != manifest['files']['InventoryMonitor-v1.11.exe']:
            raise ValueError('程序替换后校验失败。')
    except Exception:
        shutil.copy2(backup, target)
        raise
    result = dict(version=manifest['version'], target=str(target), workspace=str(workspace), backup=str(backup),
                  backendCurrent=order_bot_config.deployed(workspace), started=False)
    (target.parent / 'repair-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--package-root', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(repair(args.package_root, args.check), ensure_ascii=False, indent=2))
    except Exception as error:
        print('修复未完成：' + str(error), file=sys.stderr)
        sys.exit(1)
