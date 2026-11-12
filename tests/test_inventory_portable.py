"""核对一体包双击启动与迁移助手使用同一工作区。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from inventory_portable import configure_environment, initialize_workspace, selected_workspace
from inventory_workspace_config import resolve_data_root


class PortableWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / 'inventory' / 'v1.10.1'
        self.folder.mkdir(parents=True)
        tool = self.root / 'robot/runtime/pwsh/pwsh.exe'
        tool.parent.mkdir(parents=True)
        tool.touch()
        snapshot = self.root / 'settings/inventory.sqlite3'
        snapshot.parent.mkdir()
        snapshot.write_bytes(b'settings-snapshot')

    def test_import_only_for_first_launch(self):
        workspace = resolve_data_root(None, self.folder)
        self.assertEqual(workspace, self.root / 'workspace')
        initialize_workspace(self.folder, workspace)
        db = workspace / 'inventory.sqlite3'
        self.assertEqual(db.read_bytes(), b'settings-snapshot')
        db.write_bytes(b'new-machine-settings')
        initialize_workspace(self.folder, workspace)
        self.assertEqual(db.read_bytes(), b'new-machine-settings')

    def test_selected_workspace_wins_over_adjacent_old_database(self):
        selected = self.root / '实际工作区'
        selected.mkdir()
        (selected / 'inventory.sqlite3').touch()
        legacy = self.folder / '.local_inventory_ui_test'
        legacy.mkdir()
        (legacy / 'inventory.sqlite3').touch()
        (self.root / 'device-paths.json').write_text(json.dumps({'workspace': str(selected)}))
        self.assertEqual(resolve_data_root(None, self.folder), selected)
        self.assertEqual(resolve_data_root(str(legacy), self.folder), legacy)

    def test_missing_selected_workspace_does_not_silently_replace_it(self):
        (self.root / 'device-paths.json').write_text(json.dumps({'workspace': str(self.root/'missing')}))
        with self.assertRaisesRegex(ValueError, '原工作区不存在'):
            resolve_data_root(None, self.folder)
        self.assertFalse((self.root/'workspace').exists())

    def test_direct_launch_can_find_portable_tools(self):
        with patch.dict(os.environ, {'PATH': 'original'}):
            configure_environment(self.folder)
            first = os.environ['PATH']
            configure_environment(self.folder)
            self.assertEqual(os.environ['PATH'], first)
            self.assertEqual(first.split(os.pathsep)[0], str(self.root / 'robot/runtime/pwsh'))

    def test_non_portable_install_keeps_legacy_workspace(self):
        self.assertIsNone(selected_workspace(self.root))
        legacy = self.root / '.local_inventory_ui_test'
        legacy.mkdir()
        (legacy/'inventory.sqlite3').touch()
        self.assertEqual(resolve_data_root(None, self.root), legacy)

    def test_first_launch_preserves_imported_pause(self):
        from inventory_monitor import InventoryService
        from inventory_app import promote_to_v1
        service = InventoryService(self.root/'isolated-state')
        with service.db() as db:
            db.execute('INSERT INTO products VALUES (?,?)', ('sample', '{}'))
            service.put(db, 'enabled', False)
        promote_to_v1(service)
        with service.db() as db:
            self.assertIs(service.get(db, 'enabled'), False)
