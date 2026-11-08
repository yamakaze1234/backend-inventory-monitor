"""不同部门工作区与旧版安装兼容验证，不读取真实账号或业务数据。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from inventory_monitor import InventoryService
from inventory_workspace_config import resolve_data_root, message_data_root, workspace_settings, save_workspace_settings


class WorkspaceConfigTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp/workspace-config-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))

    def test_explicit_directory_wins_without_touching_default(self):
        selected = self.root / 'department-a'
        result = resolve_data_root(selected, self.root / 'program', local_app_data=self.root / 'local')
        self.assertEqual(result, selected.resolve())
        self.assertFalse(selected.exists())

    def test_new_installations_have_distinct_user_workspaces(self):
        first = resolve_data_root(None, self.root / 'program-a', local_app_data=self.root / 'local')
        second = resolve_data_root(None, self.root / 'program-b', local_app_data=self.root / 'local')
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_relative_to((self.root / 'local').resolve()))
        self.assertFalse(first.exists())
        self.assertEqual(first, resolve_data_root(None, self.root / 'program-a', local_app_data=self.root / 'local'))

    def test_existing_adjacent_inventory_stays_in_place(self):
        program = self.root / 'old-install'
        legacy = program / '.local_inventory_ui_test'
        service = InventoryService(legacy)
        self.assertEqual(resolve_data_root(None, program, local_app_data=self.root / 'local'), legacy.resolve())
        with service.db() as db:service.put(db, 'daily_source_root', str(self.root / 'previous-message-root'))
        self.assertEqual(message_data_root(service, legacy=True), (self.root / 'previous-message-root').resolve())

    def test_new_workspace_never_uses_global_message_data(self):
        service = InventoryService(self.root / 'new-data')
        with patch('monitor_portable_install.default_data_dir', side_effect=AssertionError('不得读取其他部门默认目录')):
            self.assertEqual(message_data_root(service), service.root.resolve())
        with service.db() as db:
            self.assertEqual(service.get(db, 'daily_source_root'), str(service.root.resolve()))

    def test_legacy_fallback_is_explicit_only(self):
        service = InventoryService(self.root / 'old-data')
        with patch('monitor_portable_install.default_data_dir', return_value=self.root / 'existing-worker'):
            self.assertEqual(message_data_root(service, legacy=True), (self.root / 'existing-worker').resolve())

    def test_empty_first_run_stays_paused_and_department_is_local(self):
        from inventory_app import promote_to_v1
        service = InventoryService(self.root / 'new-data')
        promote_to_v1(service)
        self.assertFalse(service.status()['enabled'])
        self.assertEqual(service.products(), [])
        result = save_workspace_settings(service, dict(department='采购部', name='配件监控'))
        self.assertEqual(result['department'], '采购部')
        self.assertEqual(workspace_settings(InventoryService(self.root / 'other-data'))['department'], '')
        with self.assertRaises(ValueError):save_workspace_settings(service, dict(name='x' * 81))
        self.assertEqual(workspace_settings(service), result)
