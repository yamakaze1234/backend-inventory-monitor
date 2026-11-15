"""关窗只作用于本店独立实例，失败不关闭浏览器。"""
from pathlib import Path
import json
import threading
import unittest
from unittest.mock import Mock, patch
from douyin_cws_browser import arguments, identity, close_browser
from douyin_inventory_runtime import DouyinRuntime


class CwsBrowserTests(unittest.TestCase):
    def setUp(self):
        # 直接调用采集入口的隔离测试也恢复线程上下文，避免污染后续终态测试。
        from douyin_inventory_runtime import _patrol_snapshot_saved, _patrol_stage
        for value in (_patrol_snapshot_saved, _patrol_stage):
            token = value.set(value.get())
            self.addCleanup(value.reset, token)

    def test_windows_quoted_chinese_path(self):
        self.assertIn('--user-data-dir=D:\店铺 目录', arguments('msedge.exe "--user-data-dir=D:\店铺 目录" --remote-debugging-port=49226'))

    def test_other_directory_cannot_be_closed(self):
        cfg=dict(port=49226,data_dir='D:/店铺一')
        result=Mock(returncode=0,stdout=json.dumps([dict(ProcessId=12,CommandLine='edge --user-data-dir=D:/店铺二 --remote-debugging-port=49226')]))
        with patch('douyin_cws_browser.subprocess.run',return_value=result), patch('douyin_cws_browser.owns_port',return_value=True), self.assertRaises(ValueError):
            identity(cfg)

    def test_close_requires_owned_port_and_confirms_exit(self):
        channel=Mock();channel.__enter__=Mock(return_value=channel);channel.__exit__=Mock(return_value=False)
        with patch('douyin_cws_browser.identity',return_value=(12,'ws://127.0.0.1:49226/devtools/browser/abc')), patch('douyin_cws_browser.owns_port',side_effect=[True,False]), patch('douyin_cws_browser.CdpChannel',return_value=channel):
            close_browser(dict(port=49226))
        channel.command.assert_called_once_with('Browser.close',timeout=5)

    def runtime(self):
        runtime=object.__new__(DouyinRuntime)
        runtime.store=Mock()
        runtime.store.store.return_value=dict(confirmed=True,name='技嘉',revision=1,status='完成',connection_mode='cws')
        runtime.store.restock_preview.return_value={}
        runtime.validate_connection=Mock();runtime.stop_event=threading.Event()
        runtime.store.db.return_value.__enter__=Mock(return_value=Mock());runtime.store.db.return_value.__exit__=Mock()
        worker=Mock();worker.call.return_value=dict(complete=True,products=[])
        runtime.worker=Mock(return_value=worker);runtime.notification_ready=Mock(return_value=False)
        runtime.sync_online=Mock(return_value=dict(skipped=True));runtime.notify_current=Mock(return_value=[])
        runtime.live=True
        return runtime,worker

    def test_success_closes_after_save_and_sync(self):
        runtime,worker=self.runtime()
        worker.finish_collection.side_effect=lambda: (runtime.store.accept.assert_called_once(),runtime.sync_online.assert_called_once())
        self.assertEqual(runtime.collect('shop')[0],'success')
        worker.finish_collection.assert_called_once()

    def test_failure_pending_save_and_sync_preserve_window(self):
        from douyin_export import ReportPending
        for stage,error in [('call',ValueError('采集失败')),('call',ReportPending('生成中')),('save',ValueError('保存失败')),('sync',ValueError('同步失败'))]:
            runtime,worker=self.runtime()
            target={'call':worker.call,'save':runtime.store.accept,'sync':runtime.sync_online}[stage]
            target.side_effect=error
            with self.assertRaises(type(error)):runtime.collect('shop')
            worker.finish_collection.assert_not_called()
        runtime,worker=self.runtime();runtime.sync_online.return_value=dict(ok=False)
        self.assertEqual(runtime.collect('shop')[0],'incomplete');worker.finish_collection.assert_not_called()
