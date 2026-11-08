"""每三天清理本工具的历史导出，保留最新有效表；仅移入回收站。"""
from pathlib import Path
import hashlib
import json
import re
import time
from inventory_recycle import recycle_file

PERIOD = 3 * 24 * 3600


def cleanup_due(store, *, now=None, recycler=recycle_file, downloads=None):
    # 新计划保存后接管旧三天规则；关闭新计划也不会恢复隐藏的旧清理。
    from inventory_cache import cleanup_lock
    from inventory_cache_schedule import legacy_managed
    with cleanup_lock(store.root.parent):
        if legacy_managed(store.root.parent):
            return dict(managed=True,recycled=0,errors=[],message='缓存清理由设置中的计划统一管理')
        return _cleanup_due(store,now=now,recycler=recycler,downloads=downloads)


def _cleanup_due(store, *, now=None, recycler=recycle_file, downloads=None):
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


def historical_export_groups(data_root, cutoff):
    """按归档组判断资格，证据只能在归档与对应原始文件成功回收后处理。"""
    import sqlite3
    from contextlib import closing
    root = Path(data_root) / 'douyin_inventory'
    database = root / 'inventory.sqlite3'
    if not database.is_file():return []
    def ordinary(path):
        return not any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction())
                       for p in (path,*path.parents))
    if not ordinary(root):return []
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)) as db:
        keep={Path(r[0]).resolve() for r in db.execute('SELECT export_file FROM stores') if r[0]}
    entries=[];exports=root/'exports'
    for folder in exports.iterdir() if exports.is_dir() else []:
        if not folder.is_dir() or not re.fullmatch(r'[a-f0-9]{32}',folder.name) or not ordinary(folder):continue
        files=sorted((p for p in folder.glob('*.xlsx') if re.fullmatch(r'\d{8}_\d{6}_[a-f0-9]{6}\.xlsx',p.name)),reverse=True)
        if files:keep.add(files[0].resolve())
        # 同时读取证据，恢复“归档已回收、原始文件回收失败”的未完成组。
        for evidence in folder.glob('*.json'):
            if not re.fullmatch(r'\d{8}_\d{6}_[a-f0-9]{6}\.json',evidence.name):continue
            target=evidence.with_suffix('.xlsx')
            try:
                if not ordinary(target) or not ordinary(evidence):continue
                data=json.loads(evidence.read_text(encoding='utf-8'))
                if Path(data['file']).resolve()!=target.resolve():continue
                digest=data['sha256'];exists=target.is_file()
                if exists and hashlib.sha256(target.read_bytes()).hexdigest()!=digest:continue
                original=Path(data.get('download_file',''))
                managed=root/'managed-downloads'/folder.name
                owned=((original.resolve().parent==managed.resolve() and re.fullmatch(r'[a-fA-F0-9-]{32,36}\.xlsx',original.name)) or
                       (original.resolve().parent==(Path.home()/'Downloads').resolve() and original.name.startswith('productExport-') and original.suffix=='.xlsx'))
                raw=original if owned and ordinary(original) and original.is_file() and hashlib.sha256(original.read_bytes()).hexdigest()==digest else None
                # 不认识的原始文件不采用，也不凭一份孤立 JSON 扩大清理范围。
                if not exists and raw is None and (original.is_file() or not re.fullmatch(r'[a-f0-9]{64}',str(digest))):continue
                entries.append((target if exists else None,evidence,raw,target.resolve()))
            except (OSError,ValueError,KeyError):continue
    protected=set(keep)
    for target,evidence,raw,identity in entries:
        if identity in keep:
            protected.add(evidence.resolve())
            if raw:protected.add(raw.resolve())
    groups=[]
    for target,evidence,raw,identity in entries:
        if identity in keep:continue
        archive_ok=target is not None and target.stat().st_mtime<cutoff
        raw_ok=raw is not None and raw.resolve() not in protected and raw.stat().st_mtime<cutoff
        archive=target.resolve() if archive_ok else None
        raw_candidate=raw.resolve() if raw_ok else None
        # 原表尚未到期时可独立清理有证明的旧下载，但必须保留归档证据。
        evidence_ok=(archive_ok or target is None and evidence.stat().st_mtime<cutoff) and (raw is None or raw_ok)
        proof=evidence.resolve() if evidence_ok and evidence.resolve() not in protected else None
        if archive or raw_candidate or proof:groups.append(dict(archive=archive,raw=raw_candidate,evidence=proof))
    return groups


def historical_export_files(data_root,cutoff):
    """兼容现有预览接口；顺序是归档、原始文件、最后才是归档证据。"""
    result=[];seen=set()
    for group in historical_export_groups(data_root,cutoff):
        for key in ('archive','raw','evidence'):
            path=group[key]
            if path is not None and path not in seen:result.append(path);seen.add(path)
    return result
