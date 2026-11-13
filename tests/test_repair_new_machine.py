"""修复包隔离验收，不加载真实账号或运行工作区。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('repair_new_machine', Path(__file__).resolve().parents[1] / 'tools/repair_new_machine.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class RepairTest(unittest.TestCase):
    def setUp(self):
        # 临时目录保留供测试后统一回收，不碰生产路径。
        base = Path(__file__).resolve().parents[1] / '.verification'
        base.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='repair-test-', dir=base))
        self.target = self.root / '原程序.exe'
        self.target.write_bytes(b'old')
        (self.root / 'InventoryMonitor-v1.11.exe').write_bytes(b'new')
        (self.root / 'repair-manifest.json').write_text(json.dumps(dict(version='1.11', files={
            'InventoryMonitor-v1.11.exe': hashlib.sha256(b'new').hexdigest()})), encoding='utf-8')

    def test_repair_preserves_settings_and_filename(self):
        # 更新只替换同名程序；既有配置逐字节保留并保留旧 EXE。
        settings = self.root / 'inventory.sqlite3'
        settings.write_bytes(b'original-settings')
        with patch.object(repair, 'ROOT', self.root), patch.object(repair, 'inspect_package', return_value=(self.target, self.root)), \
                patch.object(repair.order_bot_config, 'deploy') as deploy, patch.object(repair.order_bot_config, 'deployed', return_value=True):
            result = repair.repair(self.root)
        deploy.assert_called_once_with(self.root)
        self.assertEqual(self.target.read_bytes(), b'new')
        self.assertEqual(Path(result['backup']).read_bytes(), b'old')
        self.assertEqual(settings.read_bytes(), b'original-settings')
        self.assertFalse(result['started'])

    def test_backend_failure_keeps_old_executable(self):
        # 部署失败不得把新 EXE 误报成完整修复。
        with patch.object(repair, 'ROOT', self.root), patch.object(repair, 'inspect_package', return_value=(self.target, self.root)), \
                patch.object(repair.order_bot_config, 'deploy', side_effect=ValueError('busy')):
            with self.assertRaisesRegex(ValueError, 'busy'):
                repair.repair(self.root)
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertFalse((self.root / 'repair-result.json').exists())

    def test_damaged_payload_never_touches_installation(self):
        # 下载损坏时在任何安装动作之前退出。
        (self.root / 'InventoryMonitor-v1.11.exe').write_bytes(b'damaged')
        with patch.object(repair, 'ROOT', self.root), patch.object(repair, 'inspect_package') as inspect:
            with self.assertRaisesRegex(ValueError, '校验失败'):
                repair.repair(self.root)
        inspect.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'old')
