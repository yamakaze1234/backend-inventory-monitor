"""自动巡检告警回归：真实临时回执库，所有外部投递均注入模拟。"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from douyin_browser import BrowserInterrupted
from douyin_inventory import DouyinInventory
from douyin_inventory_runtime import DouyinRuntime, _patrol_snapshot_saved
from douyin_patrol_failure import CHANNEL, deliver, payload
from notification_robots import RobotStore


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.robots = RobotStore(self.root)
        with patch('monitor_secret_store.protect', side_effect=lambda x: x):
            self.robot = self.robots.save('测试群', 'https://oapi.dingtalk.com/robot/send?access_token=synthetic', [CHANNEL])
        self.runtime = object.__new__(DouyinRuntime)
        self.runtime.store = DouyinInventory(self.root)
        self.ident = self.runtime.store.add_store('技嘉旗舰店')
        self.runtime.live = True
        self.runtime.service = Mock()
        self.runtime.collection_lock = threading.Lock()
        self.runtime.pending_collections = {self.ident}
        self.runtime.due = {}
        self.runtime.stop_event = threading.Event()
        self.runtime.worker = Mock(return_value=Mock())
        self.runtime.collect = Mock(side_effect=ValueError('页面未就绪或登录失效'))

    def test_single_attempt_survives_store_reopen_for_all_delivery_outcomes(self):
        for status in ('sent', 'failed', 'unknown'):
            transport = Mock(return_value=dict(status=status, retryable=True))
            with patch('notification_robots.send_encrypted', transport):
                deliver(self.robots, status, '技嘉旗舰店', time.time(), time.time(), '已脱敏原因')
                deliver(RobotStore(self.root), status, '技嘉旗舰店', time.time(), time.time(), '已脱敏原因')
            self.assertEqual(transport.call_count, 1)

    def test_only_live_automatic_failures_notify_and_release(self):
        sender = Mock(return_value=dict(status='sent'))
        with patch('notification_robots.store_for_service', return_value=self.robots), patch('douyin_patrol_failure.deliver', sender):
            for trigger, live in (('手动', True), ('手动确认', True), ('定时', False), ('定时', True), ('重启续取', True)):
                self.runtime.live = live
                with self.assertRaises(ValueError):
                    self.runtime.collect_reserved(self.ident, trigger=trigger)
        self.assertEqual(sender.call_count, 2)
        self.assertNotIn(self.ident, self.runtime.pending_collections)
        self.assertEqual(self.runtime.store.store(self.ident)['checked'], 0)

    def test_browser_retry_only_reports_terminal_failure(self):
        self.runtime.collect.side_effect = BrowserInterrupted('本店浏览器断开')
        sender = Mock(return_value=dict(status='sent'))
        with patch('notification_robots.store_for_service', return_value=self.robots), patch('douyin_patrol_failure.deliver', sender):
            with self.assertRaises(BrowserInterrupted):
                self.runtime.collect_reserved(self.ident, trigger='定时')
        self.assertEqual(self.runtime.collect.call_count, 2)
        sender.assert_called_once()

    def test_success_and_cancellation_never_notify(self):
        sender = Mock()
        with patch('douyin_patrol_failure.deliver', sender):
            for result in (None, ('success', '已完成', '无需发送')):
                self.runtime.collect.side_effect = None
                self.runtime.collect.return_value = result
                self.runtime.collect_reserved(self.ident, trigger='定时')
        sender.assert_not_called()

    def test_post_snapshot_failure_has_distinct_message_and_context_is_reset(self):
        def collect(*args, **kwargs):
            _patrol_snapshot_saved.set(True)
            raise OSError('sensitive connection string')
        self.runtime.collect.side_effect = collect
        sender = Mock(return_value=dict(status='unknown'))
        with patch('notification_robots.store_for_service', return_value=self.robots), patch('douyin_patrol_failure.deliver', sender):
            with self.assertRaises(OSError):
                self.runtime.collect_reserved(self.ident, trigger='定时')
        self.assertTrue(sender.call_args.kwargs['snapshot_saved'])
        self.assertNotIn('sensitive', sender.call_args.args[-1])
        self.assertFalse(_patrol_snapshot_saved.get())
        text = payload('技嘉旗舰店', 0, 47, '同步失败', snapshot_saved=True)['text']['content']
        self.assertIn('库存已采集并保存', text)
        self.assertNotIn('不能作为当前库存依据', text)

    def test_notification_exception_preserves_original_and_releases(self):
        with patch('notification_robots.store_for_service', side_effect=RuntimeError('notification failed')):
            with self.assertRaisesRegex(ValueError, '登录失效'):
                self.runtime.collect_reserved(self.ident, trigger='定时')
        self.assertNotIn(self.ident, self.runtime.pending_collections)

    def test_shutdown_error_is_not_reported_as_patrol_failure(self):
        self.runtime.stop_event.set()
        with patch('douyin_patrol_failure.deliver') as sender:
            with self.assertRaises(ValueError):
                self.runtime.collect_reserved(self.ident, trigger='定时')
        sender.assert_not_called()
