"""用隔离数据库与假发送器验证恢复通知后的投递边界。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from douyin_inventory import DouyinInventory
from douyin_notification import deliver_current, delivery_label
from notification_robots import RobotStore
from tests.test_douyin_restock import item


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
        self.send(); self.accept(); self.send()
        self.store = DouyinInventory(self.root)
        self.send()
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
        self.send(); self.transport.assert_not_called()

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

    def test_failed_online_sync_does_not_offer_stale_action_link(self):
        self.accept()
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        text = self.transport.call_args.args[1]['markdown']['text']
        self.assertIn('在线明细尚未同步', text)
        self.assertNotIn('[查看在线明细', text)

    def test_retry_refreshes_online_readiness_without_new_job(self):
        self.accept()
        with self.store.db() as db:
            db.execute('UPDATE stores SET online_url=? WHERE id=?', ('https://example.com/detail', self.ident))
        self.transport.side_effect = [dict(status='failed', retryable=True), dict(status='sent')]
        self.send()
        deliver_current(self.store, self.robots, self.ident, online_ready=False)
        self.assertNotIn('[查看在线明细', self.transport.call_args.args[1]['markdown']['text'])
        with self.robots.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_label_filters_old_signature_and_disabled_recipient(self):
        self.accept(); self.send()
        self.assertEqual(delivery_label(self.store, self.ident, self.robots), '已发送')
        self.robots.set_enabled(self.one, False)
        self.assertNotEqual(delivery_label(self.store, self.ident, self.robots), '已发送')
        self.accept(3)
        self.assertNotEqual(delivery_label(self.store, self.ident), '已发送')


if __name__ == '__main__':
    unittest.main()
