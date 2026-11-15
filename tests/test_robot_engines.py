"""双引擎配置隔离测试，不启动 Stream 或读取登录资料。"""
import json
from pathlib import Path
import runpy
from unittest.mock import patch
import order_bot_config as config
from order_business import backend_form
from harness_registration import updated
from tests.test_order_department import DepartmentTests


class EngineTests(DepartmentTests):
    def harness(self):
        entry=self.root/'harness/bin.js';entry.parent.mkdir();entry.write_text('// synthetic')
        return dict(self.values,connector_mode='harness',harness_entry=str(entry),harness_home=str(self.root/'harness-home'))

    def test_engine_form_and_legacy(self):
        form=backend_form({},'test')
        self.assertEqual(form['values']['connector_mode'],'codex')
        self.assertEqual(config.normalize(self.values)['agent_model'],'')
        self.assertEqual({'harness','codex'},{r['value'] for r in form['fields'][0]['options']})

    def test_invalid_engine_and_path(self):
        with self.assertRaises(ValueError):config.normalize(dict(self.values,connector_mode='unknown'))
        with self.assertRaises(ValueError):config.normalize(dict(self.values,connector_mode='harness'))

    def test_harness_registration_and_nested_bridge_deployment(self):
        values=self.harness()
        with patch.object(config,'assert_connector_stopped'):
            config.save(self.workspace,values)
            patchfile=Path(values['harness_home'])/'profiles/web/cordis.patch.yml'
            patchfile.parent.mkdir(parents=True);patchfile.write_text('# user settings\n',encoding='utf-8')
            config.deploy(self.workspace)
            first=patchfile.read_bytes()
            config.deploy(self.workspace)
            self.assertEqual(first,patchfile.read_bytes())
            self.assertTrue(first.startswith(b'# user settings'))
            self.assertTrue((self.control/'ops-bridge/src/ops_receiver.py').exists())
            self.assertTrue(config.deployed(self.workspace))
            patchfile.write_text(patchfile.read_text(encoding='utf-8')+'# model preference\n',encoding='utf-8')
            self.assertTrue(config.deployed(self.workspace))

    def test_foreign_registration_rejected(self):
        values=self.harness()
        with self.assertRaises(ValueError):updated(values,self.workspace,'serverName: erp_daily_report\nargs: [other/report-server.mjs]\n')

    def test_engine_switch_preserves_daily_config(self):
        values=self.harness()
        with patch.object(config,'assert_connector_stopped'):
            config.save(self.workspace,values)
            daily=self.workspace/'daily-report.json';daily.write_text('synthetic-report-reference')
            config.save(self.workspace,dict(values,connector_mode='codex',agent_model='chosen-model'))
            self.assertEqual(daily.read_text(),'synthetic-report-reference')
            self.assertEqual(config.load(self.workspace)['harness_home'],values['harness_home'])

    def test_active_worker_blocks_engine_switch(self):
        config.save(self.workspace,self.values)
        with patch.object(config,'queue_state',return_value=dict(active=1,online=False,enabled=False)):
            with self.assertRaises(ValueError):config.save(self.workspace,self.harness())
        self.assertEqual(config.load(self.workspace)['connector_mode'],'codex')

    def test_harness_health_requires_owned_process(self):
        runtime=runpy.run_path(str(config.bundle_root()/'control/harness_runtime.py'))
        values=self.harness()
        self.assertEqual(runtime['status'](values,rows=[])['state'],'down')
        self.assertEqual(runtime['status'](values,rows=[dict(pid=1,created='x',owned=False)])['state'],'unmanaged')
        with self.assertRaises(ValueError):runtime['patch_stream']('// unknown plugin')
