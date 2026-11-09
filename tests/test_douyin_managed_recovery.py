"""专用巡检恢复边界：不接触真实店铺、导出任务或群通知。"""
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
import json
import tempfile
import threading
import time
import unittest

from douyin_browser import BrowserInterrupted, CdpChannel, ShopBrowser
from douyin_export import ChromeExportWorker, PRODUCT_URL, REPORT_URL, ReportPending
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_managed_export import ManagedExportWorker


class ManagedRecoveryTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'managed-recovery-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.ident = 'a' * 32
        self.worker = ManagedExportWorker(None, self.root / 'profiles' / self.ident)
        self.worker.expected_shop = '测试店铺'
        self.worker.pause = Mock()
        self.worker._browser = Mock()
        self.pending = self.root / 'chrome' / (self.ident + '.pending.json')

    def channel(self, messages):
        socket = Mock()
        socket.recv.side_effect = messages
        return CdpChannel('ws://127.0.0.1:9222/devtools/page/own', 9222,
                          connector=Mock(return_value=socket)), socket

    def test_crash_events_and_command_errors_are_not_replayed(self):
        for message in [dict(method='Inspector.targetCrashed'),
                        dict(method='Inspector.detached'),
                        dict(id=1, error=dict(message='Target crashed: private-response'))]:
            channel, socket = self.channel([json.dumps(message)])
            with self.assertRaises(BrowserInterrupted) as error:
                channel.command('Runtime.evaluate')
            self.assertNotIn('private-response', str(error.exception))
            socket.send.assert_called_once()

    def test_command_timeout_is_interrupted_but_idle_download_is_not(self):
        channel, socket = self.channel([TimeoutError()])
        with self.assertRaises(BrowserInterrupted):
            channel.command('Runtime.evaluate')
        socket.recv.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            channel.event(1)

    def test_download_channel_ignores_other_tabs_but_detects_owned_page_crash(self):
        channel, _ = self.channel([
            json.dumps(dict(method='Target.targetCrashed', params=dict(targetId='other'))),
            json.dumps(dict(method='Target.targetCrashed', params=dict(targetId='own')))])
        channel.watched_target = 'own'
        self.assertEqual(channel.event(1)['params']['targetId'], 'other')
        with self.assertRaises(BrowserInterrupted):
            channel.event(1)

    def test_recovery_replaces_only_owned_tab_and_saves_before_closing_old(self):
        browser = ShopBrowser(self.root, self.ident, 'chrome')
        browser.state = dict(port=9222, browser_id='owned', page='old-page')
        browser.running = Mock(return_value='ws://127.0.0.1:9222/devtools/browser/owned')
        browser.navigate = Mock()
        with patch('douyin_browser.CdpChannel') as factory:
            channel = factory.return_value.__enter__.return_value
            def command(method, params):
                if method == 'Target.createTarget':
                    return dict(targetId='new-page')
                self.assertEqual(params, dict(targetId='old-page'))
                self.assertEqual(json.loads(browser.state_file.read_text())['page'], 'new-page')
                return dict(success=True)
            channel.command.side_effect = command
            browser.recover(PRODUCT_URL)
            self.assertEqual([c.args[0] for c in channel.command.call_args_list],
                             ['Target.createTarget', 'Target.closeTarget'])
        browser.navigate.assert_called_once_with(PRODUCT_URL)

    def test_recovery_rejects_reused_browser_port_before_creating_tab(self):
        browser = ShopBrowser(self.root, self.ident, 'chrome', launcher=Mock())
        browser.state = dict(port=9222, browser_id='owned', page='old')
        browser.read = Mock(return_value=dict(webSocketDebuggerUrl='ws://127.0.0.1:9222/devtools/browser/other'))
        with patch('douyin_browser.CdpChannel') as channel, self.assertRaisesRegex(ValueError, '其他浏览器'):
            browser.recover(PRODUCT_URL)
        channel.assert_not_called()
        browser.launcher.assert_not_called()

    def test_same_ready_page_is_reused_with_stability_checks(self):
        self.worker.eval = Mock(return_value=True)
        self.worker.check_shop = Mock()
        self.worker.open_page(PRODUCT_URL, '#exportSearchedGoods')
        self.worker._browser.navigate.assert_not_called()
        self.worker._browser.reload.assert_not_called()
        self.assertEqual(self.worker.pause.call_count, 2)

    def test_report_refresh_waits_for_new_document_and_never_visits_product_page(self):
        self.worker.last_navigation = 99
        self.worker.eval = Mock(side_effect=[True, 123.0, False, True, True, True])
        self.worker.check_shop = Mock()
        with patch('douyin_managed_export.time.monotonic', return_value=100):
            self.worker.open_page(REPORT_URL, 'table', refresh=True)
        self.worker._browser.navigate.assert_not_called()
        self.worker._browser.reload.assert_called_once()
        self.assertEqual(self.worker.pause.call_args_list[0].args, (14,))
        self.assertIn('performance.timeOrigin !== 123.0', self.worker.eval.call_args.args[0])

    def test_pending_intent_exists_before_submit_and_crash_preserves_it(self):
        self.worker.open_page = self.worker.check_shop = self.worker.wait_export_count = Mock()
        self.worker.wait_report_records = Mock(return_value=[])
        self.worker.eval = Mock(return_value=dict(tab='全部', count='2'))
        def click(selector, text):
            if text == '导出':
                saved = self.worker.load_pending(self.pending)
                self.assertTrue(saved['submission_unknown'])
                self.assertEqual(saved['count'], 2)
                raise BrowserInterrupted('本店浏览器页面崩溃')
        self.worker.click = Mock(side_effect=click)
        with self.assertRaises(BrowserInterrupted):
            self.worker.request_export()
        self.assertTrue(self.worker.load_pending(self.pending)['submission_unknown'])

    def test_recovered_collection_resumes_unknown_submission_without_another_export(self):
        started = time.time()
        task = dict(started=started, count=2, old=[], shop='测试店铺', submission_unknown=True)
        self.worker.save_pending(self.pending, task)
        self.worker.request_export = Mock(side_effect=AssertionError('不得重复导出'))
        self.worker.refresh_reports = Mock()
        self.worker.records = Mock(return_value=[dict(key='1', ready=True,
            time=datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M:%S'))])
        self.worker.download_report = Mock(return_value=dict(complete=True))
        self.assertTrue(self.worker.call('collect')['complete'])
        self.worker.request_export.assert_not_called()
        self.assertNotIn('submission_unknown', self.worker.load_pending(self.pending))

    def test_unknown_missing_task_stops_without_resubmission(self):
        task = dict(started=time.time()-130, count=2, old=[], shop='测试店铺', submission_unknown=True)
        self.worker.save_pending(self.pending, task)
        self.worker.request_export = Mock(side_effect=AssertionError('不得重复导出'))
        self.worker.refresh_reports = Mock()
        self.worker.records = Mock(return_value=[])
        with self.assertRaisesRegex(ValueError, '提交结果未确认'):
            self.worker.call('collect')
        self.worker.request_export.assert_not_called()
        self.assertEqual(self.worker.load_pending(self.pending), task)

    def test_bad_pending_or_wrong_shop_never_becomes_empty_task(self):
        self.pending.parent.mkdir()
        for content in ('{', '[]', '{"shop":"另一店"}', '{"shop":"测试店铺","count":true}'):
            self.pending.write_text(content, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '任务记录无法核对'):
                self.worker.load_pending(self.pending)

    def test_failed_intent_write_prevents_export_click(self):
        self.worker.open_page = self.worker.check_shop = self.worker.wait_export_count = Mock()
        self.worker.wait_report_records = Mock(return_value=[])
        self.worker.eval = Mock(return_value=dict(tab='全部', count='2'))
        self.worker.click = Mock()
        self.worker.save_pending = Mock(side_effect=OSError('磁盘不可写'))
        with self.assertRaises(OSError):
            self.worker.request_export()
        self.assertNotIn('导出', [c.args[1] for c in self.worker.click.call_args_list])

    def runtime(self):
        store = DouyinInventory(self.root)
        ident = store.add_store('测试店铺')
        runtime = object.__new__(DouyinRuntime)
        runtime.store, runtime.stop_event = store, threading.Event()
        runtime.due, runtime.pending_collections = {}, {ident}
        runtime.collection_lock = threading.Lock()
        runtime.worker = Mock(return_value=self.worker)
        self.worker.recover_browser = Mock()
        return runtime, ident

    def test_one_recovery_covers_whole_round_and_keeps_single_history_record(self):
        runtime, ident = self.runtime()
        runtime.collect = Mock(side_effect=[BrowserInterrupted('本店浏览器页面崩溃'),
            ('success', '检查完成', '未发送：同一清单已发过')])
        runtime.collect_reserved(ident)
        runtime.worker.assert_called_once_with(ident)
        self.worker.recover_browser.assert_called_once()
        with runtime.store.db() as db:
            row = db.execute('SELECT outcome,detail,delivery FROM patrol_history').fetchone()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM patrol_history').fetchone()[0], 1)
        self.assertEqual(row['outcome'], 'success')
        self.assertIn('恢复本店浏览器 1 次', row['detail'])
        self.assertIn('同一清单已发过', row['delivery'])

    def test_second_crash_after_pending_wait_does_not_reset_recovery_budget(self):
        runtime, ident = self.runtime()
        runtime.stop_event = Mock()
        runtime.stop_event.is_set.return_value = False
        runtime.stop_event.wait.return_value = False
        runtime.collect = Mock(side_effect=[BrowserInterrupted('本店浏览器页面崩溃'),
            ReportPending('等待'), BrowserInterrupted('本店浏览器页面再次崩溃')])
        with self.assertRaises(BrowserInterrupted):
            runtime.collect_reserved(ident)
        self.worker.recover_browser.assert_called_once()
        self.assertNotIn(ident, runtime.pending_collections)

    def test_cancelled_shop_is_not_recovered(self):
        runtime, ident = self.runtime()
        runtime.store.select_patrol(ident, False)
        runtime.collect = Mock(side_effect=BrowserInterrupted('本店浏览器页面崩溃'))
        runtime.collect_reserved(ident)
        self.worker.recover_browser.assert_not_called()

    def test_business_error_never_triggers_browser_recovery(self):
        runtime, ident = self.runtime()
        runtime.collect = Mock(side_effect=ValueError('浏览器店铺与配置不一致'))
        with self.assertRaises(ValueError):
            runtime.collect_reserved(ident)
        self.worker.recover_browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
