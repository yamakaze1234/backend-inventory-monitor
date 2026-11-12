"""本机运营指令启动配置：显式开启、DPAPI 回读与离线预览隔离。"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from monitor_secret_store import protect
from ops_runtime import configured_api, pilot_connection


class PilotConnectionTests(unittest.TestCase):
    def test_user_bound_file_and_explicit_override(self):
        with TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            root = Path(root)
            self.assertIsNone(pilot_connection(root))
            cfg = root / 'ops-command-pilot.json'
            cfg.write_text(json.dumps(dict(robot_id='robot', org_id='org', port=19486)), encoding='utf-8')
            (root/'ops-command-key.dat').write_bytes(protect(b'x'*40))
            self.assertEqual(pilot_connection(root), ('x'*40, 'robot', 'org', 19486))
            with patch.dict(os.environ, dict(INVENTORY_OPS_TOKEN='y'*40, INVENTORY_OPS_ROBOT_ID='other',
                                             INVENTORY_OPS_ORG_ID='other-org', INVENTORY_OPS_PORT='19487')):
                self.assertEqual(pilot_connection(root), ('y'*40, 'other', 'other-org', 19487))
            cfg.write_text('{}', encoding='utf-8')
            self.assertIsNone(configured_api(SimpleNamespace(root=root, offline_preview=True), None))
            with self.assertRaisesRegex(ValueError, '连接配置无效'):
                pilot_connection(root)


if __name__ == '__main__':
    unittest.main()
