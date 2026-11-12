"""覆盖双击 EXE 的工作区选择及便携环境继承。"""
import json
import os
from pathlib import Path
from inventory_portable import configure_environment, initialize_workspace, selected_workspace
from inventory_workspace_config import resolve_data_root


def layout(tmp_path):
    exe_dir = tmp_path / 'inventory' / 'v1.08-final'
    exe_dir.mkdir(parents=True)
    pwsh = tmp_path / 'robot/runtime/pwsh/pwsh.exe'
    pwsh.parent.mkdir(parents=True)
    pwsh.touch()
    seed = tmp_path / 'settings/inventory.sqlite3'
    seed.parent.mkdir()
    seed.write_bytes(b'settings-snapshot')
    return exe_dir


def test_default_import_and_preserve_existing(tmp_path):
    folder = layout(tmp_path)
    workspace = resolve_data_root(None, folder)
    assert workspace == tmp_path / 'workspace'
    initialize_workspace(folder, workspace)
    db = workspace / 'inventory.sqlite3'
    assert db.read_bytes() == b'settings-snapshot'
    db.write_bytes(b'new-machine-settings')
    initialize_workspace(folder, workspace)
    assert db.read_bytes() == b'new-machine-settings'


def test_existing_selected_workspace_is_used(tmp_path):
    folder = layout(tmp_path)
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'inventory.sqlite3').touch()
    (tmp_path / 'device-paths.json').write_text(json.dumps({'workspace': str(existing)}))
    assert resolve_data_root(None, folder) == existing
    assert resolve_data_root(str(tmp_path / 'explicit'), folder) == tmp_path / 'explicit'


def test_missing_selected_workspace_is_not_silently_replaced(tmp_path):
    import pytest
    folder = layout(tmp_path)
    (tmp_path / 'device-paths.json').write_text(json.dumps({'workspace': str(tmp_path/'missing')}))
    with pytest.raises(ValueError, match='原工作区不存在'):
        selected_workspace(folder)


def test_child_process_path_includes_portable_tools(tmp_path, monkeypatch):
    folder = layout(tmp_path)
    monkeypatch.setenv('PATH', 'original')
    configure_environment(folder)
    first = os.environ['PATH']
    configure_environment(folder)
    assert os.environ['PATH'] == first
    assert first.split(os.pathsep)[0] == str(tmp_path / 'robot/runtime/pwsh')


def test_ordinary_install_unchanged(tmp_path):
    assert selected_workspace(tmp_path) is None


def test_legacy_workspace_keeps_priority(tmp_path):
    folder = layout(tmp_path)
    legacy = folder / '.local_inventory_ui_test'
    legacy.mkdir()
    (legacy / 'inventory.sqlite3').touch()
    assert resolve_data_root(None, folder) == legacy


def test_first_launch_preserves_imported_pause(tmp_path):
    from inventory_monitor import InventoryService
    from inventory_app import promote_to_v1
    service = InventoryService(tmp_path)
    with service.db() as db:
        db.execute('INSERT INTO products VALUES (?,?)', ('sample', '{}'))
        service.put(db, 'enabled', False)
    promote_to_v1(service)
    with service.db() as db:
        assert service.get(db, 'enabled') is False
