"""独立浏览器的归属、迁移和下载边界测试；不访问真实店铺。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch
import json
import queue
import tempfile
import unittest

from douyin_browser import ShopBrowser, CdpChannel, socket_url
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime
from douyin_managed_export import ManagedExportWorker


class BrowserTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'managed-browser-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.store = DouyinInventory(self.root)
        self.ident = self.store.add_store('测试店铺')

    def test_default_is_managed_and_each_shop_browser_has_unique_directory(self):
        other = self.store.add_store('第二家店')
        self.assertEqual(self.store.store(self.ident)['connection_mode'], 'managed')
        first = ShopBrowser(self.store.root, self.ident, 'chrome')
        second = ShopBrowser(self.store.root, other, 'chrome')
        edge = ShopBrowser(self.store.root, self.ident, 'edge')
        self.assertEqual(len({first.user_data,second.user_data,edge.user_data}), 3)
        self.assertTrue(first.user_data.is_relative_to(self.store.root))
        with self.assertRaises(ValueError):
            ShopBrowser(self.store.root, '../other', 'chrome')

    def test_identical_settings_keep_confirmed_snapshot_and_revision(self):
        self.store.rebase(self.ident, confirmed=True)
        self.store.accept(self.ident, self.store.store(self.ident)['revision'], [], complete=True, now=100)
        before = self.store.store(self.ident)
        self.assertFalse(self.store.save_browser(self.ident, 'chrome', mode='managed'))
        self.assertEqual(self.store.store(self.ident), before)

    def test_legacy_confirmed_shop_keeps_connection_and_unconfigured_uses_managed(self):
        self.store.save_browser(self.ident, 'edge', 'known-extension')
        self.store.rebase(self.ident, confirmed=True)
        other = self.store.add_store('待设置店铺')
        with self.store.db() as db:
            db.execute('ALTER TABLE stores DROP COLUMN connection_mode')
        migrated = DouyinInventory(self.root)
        self.assertEqual(migrated.store(self.ident)['connection_mode'], 'extension')
        self.assertEqual(migrated.store(self.ident)['browser_profile'], 'known-extension')
        self.assertEqual(migrated.store(other)['connection_mode'], 'managed')

    def test_remote_or_wrong_port_websocket_is_rejected(self):
        for value in ['wss://example.com/devtools/page/one','ws://127.0.0.1:99/devtools/page/a',
                      'ws://user:password@127.0.0.1:9222/devtools/page/a','ws://127.0.0.1:9222/other']:
            with self.assertRaises(ValueError):
                socket_url(value, 9222)

    def test_existing_unconfirmed_binding_is_not_migrated_to_an_empty_login(self):
        folder = self.store.root/'chrome';folder.mkdir()
        (folder/(self.ident+'.json')).write_text('{"page":"existing-page"}',encoding='utf-8')
        with self.store.db() as db:
            db.execute('ALTER TABLE stores DROP COLUMN connection_mode')
        migrated = DouyinInventory(self.root)
        self.assertEqual(migrated.store(self.ident)['connection_mode'], 'extension')

    def test_port_reuse_is_rejected_without_launching_or_attaching(self):
        browser = ShopBrowser(self.store.root, self.ident, 'chrome', launcher=Mock())
        browser.state = dict(port=9222,browser_id='original',page='page')
        browser.read = Mock(return_value=dict(webSocketDebuggerUrl='ws://127.0.0.1:9222/devtools/browser/different'))
        with self.assertRaises(ValueError):
            browser.open('about:blank')
        browser.launcher.assert_not_called()

    def test_command_keeps_download_events_and_matches_own_response(self):
        connection = Mock()
        connection.recv.side_effect = [json.dumps(dict(method='Browser.downloadWillBegin',params={'guid':'sample'})),
                                       json.dumps(dict(id=1,result={'value':'correct'}))]
        channel = CdpChannel('ws://127.0.0.1:9222/devtools/browser/one',9222,connector=Mock(return_value=connection))
        self.assertEqual(channel.command('Browser.getVersion'), {'value':'correct'})
        self.assertEqual(channel.event(1)['params']['guid'], 'sample')

    def test_three_managed_connections_need_no_extension_ids(self):
        runtime = object.__new__(DouyinRuntime); runtime.store = self.store
        for name in ['第二店','第三店']:
            self.store.add_store(name)
        for config in self.store.stores():
            self.store.rebase(config['id'],confirmed=True)
            runtime.validate_connection(self.store.store(config['id']))

    def test_failed_reconnect_retains_existing_inventory(self):
        self.store.rebase(self.ident,confirmed=True)
        self.store.accept(self.ident,self.store.store(self.ident)['revision'],[],complete=True,now=100)
        runtime = object.__new__(DouyinRuntime)
        runtime.store, runtime.workers = self.store, {}
        worker = Mock(); worker.call.side_effect = ValueError('登录已失效')
        runtime.worker = Mock(return_value=worker)
        before = self.store.store(self.ident)
        with self.assertRaises(ValueError):
            runtime.execute('login',self.ident)
        self.assertEqual(self.store.store(self.ident),before)

    def test_managed_settings_are_queued_and_do_not_launch_on_save(self):
        runtime = object.__new__(DouyinRuntime)
        runtime.store, runtime.workers = self.store, {}
        runtime.worker, runtime.sync_online = Mock(), Mock()
        reply = queue.Queue()
        runtime.execute(dict(kind='browser_settings',value=dict(mode='managed',browser='chrome'),open=False,reply=reply), self.ident)
        self.assertTrue(reply.get_nowait()['ok'])
        runtime.worker.assert_not_called()

    def test_failed_open_still_syncs_invalidated_reminders(self):
        runtime = object.__new__(DouyinRuntime)
        runtime.store, runtime.workers = self.store, {}
        runtime.sync_online = Mock()
        worker = Mock();worker.call.side_effect = ValueError('浏览器未安装')
        runtime.worker = Mock(return_value=worker)
        with self.assertRaises(ValueError):
            runtime.execute(dict(kind='browser_settings',value=dict(mode='managed',browser='edge'),open=True,reply=queue.Queue()),self.ident)
        runtime.sync_online.assert_called_once_with(self.ident)

    def test_click_waits_for_missing_and_disabled_button_then_clicks_once(self):
        # 页面尚未渲染、列表加载期间禁用，均等待条件就绪再结束。
        worker = ManagedExportWorker(None, self.store.root/'profiles'/self.ident)
        worker.check_shop = Mock()
        worker.pause = Mock()
        worker.eval = Mock(side_effect=[dict(state='waiting', reason='missing'),
            dict(state='waiting', reason='disabled'), dict(state='clicked')])
        worker.click('#exportSearchedGoods', '导出查询商品')
        self.assertEqual(worker.eval.call_count, 3)
        self.assertEqual(worker.pause.call_count, 2)

    def test_click_rejects_ambiguous_or_changed_shop_without_retry(self):
        # 不用自动重试绕过多个按钮或店铺身份变化。
        for state in ('ambiguous', 'wrong_shop'):
            worker = ManagedExportWorker(None, self.store.root/'profiles'/self.ident)
            worker.check_shop, worker.pause = Mock(), Mock()
            worker.eval = Mock(return_value=dict(state=state))
            with self.assertRaises(ValueError):
                worker.click('#exportSearchedGoods', '导出查询商品')
            worker.eval.assert_called_once()
            worker.pause.assert_not_called()

    def test_click_timeout_explains_disabled_state(self):
        worker = ManagedExportWorker(None, self.store.root/'profiles'/self.ident)
        worker.check_shop, worker.pause = Mock(), Mock()
        worker.eval = Mock(return_value=dict(state='waiting', reason='disabled'))
        with self.assertRaisesRegex(ValueError, '控件仍被禁用'):
            worker.click('#exportSearchedGoods', '导出查询商品', seconds=0)
        worker.pause.assert_not_called()

    def test_cold_start_timeout_retries_same_owned_browser(self):
        browser = ShopBrowser(self.store.root, self.ident, 'chrome')
        browser.state = dict(port=9222, browser_id='owned-browser', page='own-page')
        url = 'ws://127.0.0.1:9222/devtools/browser/owned-browser'
        browser.read = Mock(side_effect=[TimeoutError('启动暂时繁忙'), dict(webSocketDebuggerUrl=url)])
        with patch('douyin_browser.time.sleep'):
            self.assertEqual(browser.running(), url)
        self.assertEqual(browser.read.call_count, 2)

    def test_close_uses_only_verified_dedicated_browser_and_preserves_login(self):
        browser = ShopBrowser(self.store.root, self.ident, 'chrome')
        browser.state = dict(port=9222, browser_id='owned-browser', page='owned-page')
        browser.running = Mock(side_effect=['ws://127.0.0.1:9222/devtools/browser/owned-browser', None])
        browser.save = Mock()
        with patch('douyin_browser.CdpChannel') as channel:
            browser.close_after_collection()
            channel.return_value.__enter__.return_value.command.assert_called_once_with('Browser.close', timeout=5)
        self.assertEqual(browser.state, {})
        browser.save.assert_called_once()

    def test_close_refuses_reused_browser_port(self):
        browser = ShopBrowser(self.store.root, self.ident, 'chrome')
        browser.running = Mock(side_effect=ValueError('端口已被其他浏览器占用'))
        with patch('douyin_browser.CdpChannel') as channel:
            with self.assertRaises(ValueError):
                browser.close_after_collection()
            channel.assert_not_called()

    def test_collect_closes_only_after_successful_snapshot_acceptance(self):
        runtime = object.__new__(DouyinRuntime)
        runtime.store = MagicMock()
        runtime.store.store.return_value = dict(confirmed=True, enabled=True, name='测试店铺', revision=1, status='查询成功')
        runtime.store.restock_preview.return_value = None
        runtime.validate_connection = Mock()
        runtime.stop_event = Mock(); runtime.stop_event.is_set.return_value = False
        runtime.notification_ready = Mock(return_value=False)
        runtime.notify_current = Mock()
        worker = Mock(); worker.call.return_value = dict(products=[], complete=True)
        runtime.worker = Mock(return_value=worker); runtime.workers = {self.ident: worker}
        order = []
        runtime.store.accept.side_effect = lambda *a, **k: order.append('accept')
        worker.finish_collection.side_effect = lambda: order.append('close')
        runtime.sync_online = Mock(side_effect=lambda *a, **k: order.append('sync') or dict(ok=True, signature='synced-preview'))
        runtime.collect(self.ident)
        self.assertEqual(order, ['accept', 'close', 'sync'])
        runtime.sync_online.assert_called_once_with(self.ident, force=False)
        runtime.notification_ready.return_value = True
        runtime.sync_online.reset_mock()
        runtime.collect(self.ident)
        runtime.sync_online.assert_called_once_with(self.ident, force=True)
        runtime.notify_current.assert_called_with(self.ident, online_ready=True, online_signature='synced-preview')
        worker.finish_collection.reset_mock()
        worker.call.side_effect = ValueError('采集失败，需要保留现场')
        with self.assertRaises(ValueError):
            runtime.collect(self.ident)
        worker.finish_collection.assert_not_called()

    def test_download_ignores_other_frame_and_requires_completion(self):
        worker = ManagedExportWorker(None, self.store.root/'profiles'/self.ident)
        worker.download_folder = self.root/'downloads'; worker.download_folder.mkdir()
        worker.download_frame = 'own-frame'
        guid = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        other = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        source = worker.download_folder/guid;source.write_bytes(b'offline-workbook')
        channel = Mock()
        channel.event.side_effect = [
            dict(method='Browser.downloadWillBegin',params=dict(frameId='another-frame',guid=other,suggestedFilename='productExport-other.xlsx')),
            dict(method='Browser.downloadProgress',params=dict(guid=other,state='completed')),
            dict(method='Browser.downloadWillBegin',params=dict(frameId='own-frame',guid=guid,suggestedFilename='productExport-own.xlsx')),
            dict(method='Browser.downloadProgress',params=dict(guid=guid,state='completed'))]
        worker.download = channel
        result = worker.wait_download(2)
        self.assertEqual(Path(result['filename']).read_bytes(),b'offline-workbook')
        self.assertEqual(Path(result['filename']).name,guid+'.xlsx')
        channel.close.assert_called_once()

    def test_managed_download_cleanup_keeps_current_export_and_browser_directory(self):
        import hashlib,os,time
        from douyin_export_cleanup import cleanup_due,PERIOD
        now=time.time()
        exports=self.store.root/'exports'/self.ident;exports.mkdir(parents=True)
        downloads=self.store.root/'managed-downloads'/self.ident;downloads.mkdir(parents=True)
        old=exports/'20260101_120000_abcdef.xlsx';latest=exports/'20260102_120000_abcdef.xlsx'
        source=downloads/'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.xlsx'
        for path in (old,latest,source):
            path.write_bytes(b'offline');os.utime(path,(now-PERIOD-10,now-PERIOD-10))
        old.with_suffix('.json').write_text(json.dumps(dict(file=str(old),download_file=str(source),sha256=hashlib.sha256(b'offline').hexdigest())),encoding='utf-8')
        (self.store.root/'export-cleanup.json').write_text(json.dumps(dict(last_run=now-PERIOD)),encoding='utf-8')
        recycler=Mock()
        result=cleanup_due(self.store,now=now,recycler=recycler)
        self.assertEqual(result['recycled'],3)
        self.assertEqual([c.args[0] for c in recycler.call_args_list],[source,old,old.with_suffix('.json')])
        self.assertNotIn(latest,[c.args[0] for c in recycler.call_args_list])


if __name__ == '__main__':
    unittest.main()
