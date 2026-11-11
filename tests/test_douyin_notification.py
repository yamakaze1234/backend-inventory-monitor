"""用隔离数据库与假发送器验证恢复通知后的投递边界。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from douyin_inventory import DouyinInventory
from douyin_notification import deliver_current, delivery_label
from notification_robots import RobotStore
from test_douyin_restock import item


class NotificationTests(unittest.TestCase):
    def setUp(self):
        # 测试文件保留在本项目临时目录，收尾统一移入回收站。
        base = Path(__file__).resolve().parents[1] / '.codex_tmp' / 'notification-rc26-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))
        self.store = DouyinInventory(self.root)
        self.robots = RobotStore(self.root)
        self.ident = self.store.add_store('测试店铺')
        self.store.rebase(self.ident, confirmed=True)
        with self.store.db() as db:
            db.execute('UPDATE stores SET enabled=1 WHERE id=?', (self.ident,))
        self.one = self.robot('目标群')
        self.transport = Mock(return_value=dict(status='sent'))
        self.patch = patch('notification_robots.send_encrypted', self.transport)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def robot(self, name, enabled=True, channels=None):
        # 不创建或解密真实凭据，路由层只收到测试占位值。
        with self.robots.db() as db:
            db.execute('INSERT INTO robots VALUES (?,?,?,?,?,?,0,?)',
                       (name, name, b'test', int(enabled), json.dumps(channels or ['douyin_inventory']),
                        json.dumps({'douyin_inventory': 1, 'inventory': 1}), name))
        return name

    def accept(self, quantity=0, ident=None):
        ident = ident or self.ident
        row = item(quantity=quantity)
        row['export_sellable'] = quantity
        self.store.accept(ident, self.store.store(ident)['revision'],
                          [dict(product_id=row['product_id'], product_name=row['product_name'],
                                monitor_scope=row['monitor_scope'], skus=[row])], complete=True)

    def send(self, ident=None):
        return deliver_current(self.store, self.robots, ident or self.ident)

    def test_current_first_round_sends_once_and_restart_keeps_dedupe(self):
        self.accept()
        first = self.send()
        self.accept()
        duplicate = self.send()
        self.store = DouyinInventory(self.root)
        self.send()
        from douyin_inventory_runtime import _patrol_delivery
        config = self.store.store(self.ident)
        self.assertEqual(_patrol_delivery(config, True, 1, first), '本轮已发送')
        self.assertEqual(_patrol_delivery(config, True, 1, duplicate), '未发送：同一清单已发过')
        self.assertEqual(_patrol_delivery(config, True, 0, []), '未发送：无符合条件的 SKU')
        self.assertEqual(self.transport.call_count, 1)
        self.assertIn('1 个链接', self.transport.call_args.args[1]['markdown']['text'])

    def test_scope_pause_and_new_recipient_do_not_resend_to_existing(self):
        self.robot('禁用群', enabled=False)
        self.robot('其他频道', channels=['inventory'])
        self.accept(); self.send()
        self.robot('第二群'); self.send()
        self.assertEqual(self.transport.call_count, 2)
        self.robots.set_enabled(self.one, False)
        self.send(); self.assertEqual(self.transport.call_count, 2)
        self.robots.set_enabled(self.one, True)
        self.send(); self.assertEqual(self.transport.call_count, 3)

    def test_unknown_and_nonretryable_failure_never_repeat(self):
        for status in ('unknown', 'failed'):
            self.transport.return_value = dict(status=status, retryable=False)
            self.accept(); self.send(); before = self.transport.call_count
            self.send(); self.assertEqual(self.transport.call_count, before)
            self.accept(3); self.send()

    def test_retry_reuses_job_and_only_retries_failed_robot(self):
        self.robot('第二群')
        self.transport.side_effect = [dict(status='sent'), dict(status='failed', retryable=True), dict(status='sent')]
        self.accept(); self.send(); self.send()
        self.assertEqual(self.transport.call_count, 3)
        with self.robots.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)

    def test_empty_resets_cycle_and_history_is_not_replayed(self):
        self.accept(); self.accept(3); self.send()
        self.transport.assert_not_called()
        self.accept(); self.send(); self.accept(3); self.send(); self.accept(); self.send()
        self.assertEqual(self.transport.call_count, 2)

    def test_paused_shop_and_invalidated_snapshot_do_not_send(self):
        self.accept()
        with self.store.db() as db:
            db.execute('UPDATE stores SET enabled=0 WHERE id=?', (self.ident,))
        self.send(); self.transport.assert_not_called()
        self.store.save_browser(self.ident, 'edge')
        deliver_current(self.store, self.robots, self.ident, manual=True)
        self.transport.assert_not_called()

    def test_manual_patrol_sends_for_paused_shop_and_records_actual_delivery(self):
        from douyin_inventory_runtime import DouyinRuntime
        from douyin_online import preview_signature

        self.store.enable(self.ident, False)
        runtime = object.__new__(DouyinRuntime)
        runtime.store, runtime.live, runtime.service = self.store, True, Mock()
        runtime.stop_event, runtime.collection_lock = threading.Event(), threading.Lock()
        runtime.due, runtime.pending_collections = {}, set()
        runtime.validate_connection = Mock()
        worker = Mock()
        row = dict(item(), export_sellable=0)
        product = dict(product_id=row['product_id'], product_name=row['product_name'],
                       monitor_scope=row['monitor_scope'], skus=[row])
        worker.call.return_value = dict(products=[product], complete=True, field='export_sellable')
        runtime.worker = Mock(return_value=worker)
        runtime.sync_online = lambda ident, **kwargs: dict(
            ok=True, signature=preview_signature(self.store.restock_preview(ident)))

        # 模拟真实“立即检查”入口，两轮相同清单只发一次，定时开关仍为暂停。
        with patch('notification_robots.store_for_service', return_value=self.robots), \
                patch('douyin_inventory_runtime.attach_maintenance'):
            runtime.execute('inspect', self.ident)
            runtime.execute('inspect', self.ident)

        self.assertFalse(self.store.store(self.ident)['enabled'])
        self.assertEqual([row['delivery'] for row in self.store.patrol_history()],
                         ['未发送：同一清单已发过', '本轮已发送'])
        self.transport.assert_called_once()

    def test_paused_shop_online_retry_releases_only_pending_manual_patrol(self):
        self.accept()
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        with self.store.db() as db:
            db.execute('UPDATE stores SET enabled=0 WHERE id=?', (self.ident,))
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        self.transport.assert_not_called()

        deliver_current(self.store, self.robots, self.ident, manual=True, online_ready=False)
        self.transport.assert_not_called()
        self.store = DouyinInventory(self.root)
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        self.transport.assert_called_once()

    def test_shop_identity_is_part_of_dedupe(self):
        other = self.store.add_store('第二店铺')
        self.store.rebase(other, confirmed=True)
        with self.store.db() as db:
            db.execute('UPDATE stores SET enabled=1 WHERE id=?', (other,))
        self.accept(); self.accept(ident=other); self.send(); self.send(other)
        self.assertEqual(self.transport.call_count, 2)

    def test_interrupted_send_is_not_replayed(self):
        self.accept()
        self.transport.side_effect = RuntimeError('模拟超时')
        self.send(); self.send()
        self.assertEqual(self.transport.call_count, 1)

    def test_failed_online_sync_waits_until_readback_and_preserves_job(self):
        self.accept()
        with self.store.db() as db:
            db.execute('UPDATE stores SET online_url=? WHERE id=?', ('https://example.com/detail', self.ident))
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        self.transport.assert_not_called()
        self.assertEqual(delivery_label(self.store, self.ident, self.robots), '等待在线明细同步，尚未发送')
        with self.store.db() as db:
            pending = json.loads(db.execute("SELECT value FROM preferences WHERE key LIKE 'douyin_delivery:%'").fetchone()[0])
        # 重启与再次同步失败不消耗发送任务；成功后同一任务只发一次。
        self.store = DouyinInventory(self.root)
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        self.assertEqual(self.transport.call_count, 1)
        self.assertIn('[查看在线明细', self.transport.call_args.args[1]['markdown']['text'])
        with self.store.db() as db:
            sent = json.loads(db.execute("SELECT value FROM preferences WHERE key LIKE 'douyin_delivery:%'").fetchone()[0])
        self.assertEqual(pending['job'], sent['job'])

    def test_retry_waits_for_online_sync_without_new_job(self):
        self.accept()
        self.transport.side_effect = [dict(status='failed', retryable=True), dict(status='sent')]
        self.send()
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        self.assertEqual(self.transport.call_count, 1)
        self.send()
        self.assertEqual(self.transport.call_count, 2)
        with self.robots.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_manual_sync_does_not_replay_history_or_unknown_send(self):
        self.accept()
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        self.transport.assert_not_called()
        self.transport.return_value = dict(status='unknown', retryable=False)
        self.send()
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        self.assertEqual(self.transport.call_count, 1)

    def test_empty_preview_resets_pending_without_sending(self):
        self.accept()
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        self.accept(3)
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        deliver_current(self.store, self.robots, self.ident, pending_only=True)
        self.transport.assert_not_called()

    def test_online_write_timeout_is_read_back_before_message(self):
        from douyin_online import FIELD_NAMES, sync_online, preview_signature
        from test_douyin_online import FakeTable
        self.accept()
        binding = dict(base_id='base', table_id='table', url='https://example.com/current',
                       profile='test', fields={name: name for name in FIELD_NAMES})
        (self.store.root / 'online-table.json').write_text(json.dumps(binding), encoding='utf-8')
        cloud = FakeTable(); cloud.unknown_once = True
        first = sync_online(self.store, self.ident, force=True, client=cloud)
        self.assertFalse(first['ok'])
        deliver_current(self.store, self.robots, self.ident, online_ready=first['ok'])
        self.transport.assert_not_called()
        second = sync_online(self.store, self.ident, force=True, client=cloud)
        self.assertTrue(second['ok'])
        deliver_current(self.store, self.robots, self.ident, online_ready=second['ok'], pending_only=True, online_signature=second['signature'])
        self.assertEqual(cloud.writes, 1)
        self.assertEqual(self.transport.call_count, 1)
        self.assertIn(binding['url'], self.transport.call_args.args[1]['markdown']['text'])

    def test_online_receipt_cannot_release_a_newer_preview(self):
        from douyin_online import FIELD_NAMES, sync_online, preview_signature
        from test_douyin_online import FakeTable
        self.accept()
        binding = dict(base_id='base', table_id='table', url='https://example.com/current',
                       profile='test', fields={name: name for name in FIELD_NAMES})
        (self.store.root / 'online-table.json').write_text(json.dumps(binding), encoding='utf-8')
        old_signature = preview_signature(self.store.restock_preview(self.ident))
        cloud = FakeTable()
        original_query = cloud.query
        reads = []
        def changing_query():
            result = original_query()
            reads.append(True)
            if len(reads) == 2:
                # 模拟同步旧清单期间另一入口保存规则并重建当前清单。
                self.store.save_low_stock_rules(self.ident, dict(default=2, parts={}))
            return result
        cloud.query = changing_query
        synced = sync_online(self.store, self.ident, force=True, client=cloud)
        self.assertTrue(synced['ok'])
        self.assertEqual(old_signature, synced['signature'])
        self.assertNotEqual(old_signature, preview_signature(self.store.restock_preview(self.ident)))
        deliver_current(self.store, self.robots, self.ident, online_signature=synced['signature'])
        self.transport.assert_not_called()
        self.assertEqual('等待在线明细同步，尚未发送', delivery_label(self.store, self.ident, self.robots))
        current = sync_online(self.store, self.ident, force=True, client=cloud)
        deliver_current(self.store, self.robots, self.ident, pending_only=True, online_signature=current['signature'])
        self.assertEqual(1, self.transport.call_count)

    def test_manual_online_action_only_releases_waiting_messages_on_success(self):
        from douyin_inventory_runtime import DouyinRuntime
        runtime = object.__new__(DouyinRuntime)
        runtime.store = self.store
        runtime.sync_online = Mock(return_value=dict(ok=False))
        runtime.notify_current = Mock()
        runtime.execute('online', self.ident)
        runtime.notify_current.assert_not_called()
        runtime.sync_online.return_value = dict(ok=True, signature='synced-preview')
        runtime.execute('online', self.ident)
        runtime.notify_current.assert_called_once_with(self.ident, online_ready=True, pending_only=True, online_signature='synced-preview')

    def test_label_filters_old_signature_and_disabled_recipient(self):
        self.accept(); self.send()
        self.assertEqual(delivery_label(self.store, self.ident, self.robots), '已发送')
        self.robots.set_enabled(self.one, False)
        self.assertNotEqual(delivery_label(self.store, self.ident, self.robots), '已发送')
        self.accept(3)
        self.assertNotEqual(delivery_label(self.store, self.ident), '已发送')


if __name__ == '__main__':
    unittest.main()
