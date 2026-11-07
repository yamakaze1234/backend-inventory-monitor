"""每三天清理本工具的历史导出，保留最新有效表；仅移入回收站。"""
from pathlib import Path
import hashlib
import json
import re
import time
from inventory_recycle import recycle_file

PERIOD = 3 * 24 * 3600


def cleanup_due(store, *, now=None, recycler=recycle_file, downloads=None):
    """清理失败留档并提示，不退回永久删除，不扫描用户的其他下载文件。"""
    now = time.time() if now is None else now
    state_file = store.root / 'export-cleanup.json'
    try:
        state = json.loads(state_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {'last_run': now, 'recycled': 0, 'errors': []}
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        return state
    if now - state.get('last_run', now) < PERIOD:
        return state
    root = (store.root / 'exports').resolve()
    downloads = Path(downloads or Path.home() / 'Downloads').resolve()
    keep = {Path(s['export_file']).resolve() for s in store.stores() if s.get('export_file')}
    recycled, errors = 0, []
    if root.exists():
        for folder in root.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', folder.name):
                continue
            files = sorted(folder.glob('*.xlsx'), key=lambda p: p.name, reverse=True)
            if files:
                keep.add(files[0].resolve())
            for target in files:
                evidence = target.with_suffix('.json')
                if (target.resolve() in keep or target.is_symlink() or evidence.is_symlink()
                        or not re.fullmatch(r'\d{8}_\d{6}_[a-f0-9]{6}\.xlsx', target.name)
                        or target.stat().st_mtime > now - PERIOD):
                    continue
                try:
                    data = json.loads(evidence.read_text(encoding='utf-8'))
                    # 元数据、路径和哈希三者一致才清理；被用户改过的表格保持原样。
                    if Path(data['file']).resolve() != target.resolve() or target.resolve().parent != folder.resolve():
                        continue
                    if hashlib.sha256(target.read_bytes()).hexdigest() != data['sha256']:
                        continue
                    original = Path(data.get('download_file', ''))
                    managed = (store.root / 'managed-downloads' / folder.name).resolve()
                    own_managed = (managed.is_relative_to(store.root.resolve()) and original.resolve().parent == managed
                                   and bool(re.fullmatch(r'[a-fA-F0-9-]{32,36}\.xlsx', original.name)))
                    if (original.is_file() and not original.is_symlink() and original.resolve().parent == downloads
                            and original.name.startswith('productExport-') and original.suffix.lower() == '.xlsx'
                            and original.stat().st_mtime <= now - PERIOD
                            and hashlib.sha256(original.read_bytes()).hexdigest() == data['sha256']):
                        recycler(original); recycled += 1
                    elif (original.is_file() and not original.is_symlink() and own_managed
                          and original.stat().st_mtime <= now - PERIOD
                          and hashlib.sha256(original.read_bytes()).hexdigest() == data['sha256']):
                        recycler(original); recycled += 1
                    recycler(target); recycled += 1
                    recycler(evidence); recycled += 1
                except Exception:
                    errors.append(target.name + '：无法移入回收站，已保留剩余文件，未强制删除')
    state = dict(last_run=now, recycled=recycled, errors=errors)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    return state
