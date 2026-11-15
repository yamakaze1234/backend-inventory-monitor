"""隔离配置与升级验证：仅合成文件，不打开真实 DBX 数据。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import daily_report_config as config
from daily_report_registration import merge
from tests.test_order_department import DepartmentTests


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='daily-config-'))
        self.value=dict(version=1,connectionId='fixture',connectionName='测试连接',database='ERP',networkMode='direct',dbxExecutable=str(self.root/'dbx.exe'),dbxData=str(self.root/'data'),python=str(self.root/'python.exe'))

    def test_missing_has_no_default(self):
        self.assertIsNone(config.load(self.root))
        self.assertFalse((self.root/'daily-report.json').exists())

    def test_save_repeat_and_backup(self):
        config.save(self.root,self.value);config.save(self.root,self.value)
        self.assertFalse((self.root/'daily-report-backups').exists())
        config.save(self.root,dict(self.value,connectionName='新连接'))
        self.assertEqual(1,len(list((self.root/'daily-report-backups').iterdir())))

    def test_bad_import_and_bad_existing_preserved(self):
        config.save(self.root,self.value)
        with self.assertRaises(ValueError):config.save(self.root,dict(self.value,password='never-store'))
        p=self.root/'daily-report.json';p.write_text('broken')
        with self.assertRaises(ValueError):config.save(self.root,self.value)
        self.assertEqual('broken',p.read_text())

    def test_replace_failure_preserves_active(self):
        config.save(self.root,self.value)
        with patch.object(Path,'replace',side_effect=OSError('simulated')):
            with self.assertRaises(OSError):config.save(self.root,dict(self.value,connectionName='new'))
        self.assertEqual(self.value,config.load(self.root))

    def test_unsupported_version(self):
        with self.assertRaises(ValueError):config.save(self.root,dict(self.value,version=2))

    def test_registration_preserves_unrelated_tools(self):
        old='[mcp_servers.other]\ncommand="keep"\n[mcp_servers.erp_daily_report]\ncommand="old"\n'
        new='[mcp_servers.erp_daily_report]\ncommand="new"\n'
        self.assertIn('command="keep"',merge(old,new))
        self.assertEqual(merge(old,new),merge(merge(old,new),new))


class UpgradeTests(DepartmentTests):
    def test_deploy_preserves_report_config_and_dbx_sentinel(self):
        import order_bot_config as backend
        backend.save(self.workspace,self.values)
        saved=b'{"independent":"unchanged"}'
        (self.workspace/'daily-report.json').write_bytes(saved)
        sentinel=self.root/'dbx.db';sentinel.write_bytes(b'synthetic-only')
        backend.deploy(self.workspace);backend.deploy(self.workspace)
        self.assertEqual(saved,(self.workspace/'daily-report.json').read_bytes())
        self.assertEqual(b'synthetic-only',sentinel.read_bytes())
        self.assertTrue((self.control/'report-server.mjs').exists())
