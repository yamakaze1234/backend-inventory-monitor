"""CWS 正式适配的店铺隔离、结果校验和调度兼容。"""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from douyin_cws_worker import CwsWorker, binding
from douyin_export import ReportPending
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_online import sync_online


class CwsWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DouyinInventory(self.root)
        self.ident = self.store.add_store('技嘉旗舰店')
        exe=self.root/'cws.exe';exe.touch()
        self.cfg=dict(executable=str(exe), workspace=str(self.root/'cws-shop'),
                      data_dir=str(self.root/'browser'), profile='DY-技嘉旗舰店', port=49226)
        self.save({self.ident:self.cfg})
        self.worker=CwsWorker(None,self.store.root/'profiles'/self.ident)
        self.worker.expected_shop='技嘉旗舰店'

    def save(self, stores):
        (self.root/'cws.json').write_text(json.dumps({'stores':stores}),encoding='utf-8')

    def result(self, result, shop='技嘉旗舰店'):
        file=Path(self.cfg['workspace'])/'out/result.json';file.parent.mkdir(parents=True,exist_ok=True)
        file.write_text(json.dumps({'records':[{'店铺':shop,'结果JSON':json.dumps(result)}]}),encoding='utf-8')
        self.worker._run=Mock(return_value=(0,json.dumps({'file':str(file),'failed':[]})))

    def test_valid_result_and_per_shop_state(self):
        self.result(dict(ok=True,complete=True,products=[]))
        with patch('douyin_cws_worker.install_scripts'):
            result=self.worker.call('collect')
        self.assertTrue(result['complete'])
        command=self.worker._run.call_args.args[0]
        options=json.loads(command[command.index('--options')+1])
        self.assertIn(self.ident,options['状态目录'])
        self.assertEqual(options['店铺'],'技嘉旗舰店')

    def test_wrong_shop_incomplete_and_nonzero_exit_rejected(self):
        for result,shop,code in [(dict(ok=True,complete=True),'另一店',0),(dict(ok=True,complete=False),'技嘉旗舰店',0),(dict(ok=True,complete=True),'技嘉旗舰店',1)]:
            self.result(result,shop)
            stdout=self.worker._run.return_value[1]
            self.worker._run.return_value=(code,stdout)
            with patch('douyin_cws_worker.install_scripts'),self.assertRaises(ValueError):
                self.worker.call('collect')

    def test_pending_report_uses_existing_scheduler_wait(self):
        self.result(dict(pending=True))
        with patch('douyin_cws_worker.install_scripts'),self.assertRaises(ReportPending):
            self.worker.call('collect')

    def test_browser_resource_collision_rejected(self):
        for field in ('port','workspace','data_dir'):
            other=dict(self.cfg,workspace=str(self.root/'other'),data_dir=str(self.root/'other-browser'),port=49227)
            other[field]=self.cfg[field]
            self.save({self.ident:self.cfg,'other':other})
            with self.assertRaises(ValueError):binding(self.root,self.ident)

    def test_mode_switch_preserves_snapshot_until_success(self):
        self.store.rebase(self.ident,confirmed=True)
        self.store.accept(self.ident,self.store.store(self.ident)['revision'],
                          [dict(product_id='123',skus=[dict(sku_id='456',export_sellable=5)])],complete=True)
        before=self.store.snapshots(self.ident)
        self.store.save_browser(self.ident,'edge',mode='cws')
        self.assertEqual(before,self.store.snapshots(self.ident))
        self.assertFalse(self.store.store(self.ident)['enabled'])

    def test_runtime_selects_cws_worker(self):
        self.store.save_browser(self.ident,'edge',mode='cws')
        runtime=object.__new__(DouyinRuntime)
        runtime.store=self.store;runtime.workers={};runtime.tool_root=self.root
        from douyin_export import ChromeMachineExportWorker
        runtime.worker_factory=ChromeMachineExportWorker
        self.assertIsInstance(runtime.worker(self.ident),CwsWorker)

    def test_unconfigured_optional_online_sync_is_skipped(self):
        self.assertTrue(sync_online(self.store,self.ident)['skipped'])
