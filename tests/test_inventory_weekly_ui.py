"""Offline Tk tests: no collection, AI calls or message delivery."""
import importlib.util
import pathlib
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch


class WeeklyUITests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('inventory_weekly_ui'), 'weekly UI module required')
        import inventory_weekly_ui
        self.ui = inventory_weekly_ui
        self.preview_patch=patch.object(self.ui,'_open_preview')
        self.preview=self.preview_patch.start()
        self.addCleanup(self.preview_patch.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.panel = self.ui.build_section(self.root, object())

    def pump_until(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('UI did not finish within three seconds')

    def test_generation_runs_in_worker_and_opens_preview_without_sending(self):
        main = threading.get_ident()
        calls = []
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(service, period, progress):
            calls.append(period)
            self.assertNotEqual(main, threading.get_ident())
            self.assertEqual(period, '上周')
            progress('正在整理周报')
            entered.set()
            release.wait(2)
            return pathlib.Path(__file__)
        with patch.object(self.ui, '_generate', generate), patch.object(self.ui.os, 'startfile', create=True) as opened:
            self.panel.period.set('上周')
            self.panel.start()
            self.assertTrue(entered.wait(1))
            self.panel.start()  # duplicate click must not start another job
            self.pump_until(lambda: self.panel.notice.get() == '正在整理周报')
            release.set()
            self.pump_until(lambda: not self.panel.running)
            self.assertIn('已生成', self.panel.notice.get())
            self.assertEqual(calls, ['上周'])
            opened.assert_not_called()
            self.preview.assert_called_once()
            self.panel.open_report()
            self.assertEqual(self.preview.call_count,2)
            opened.assert_not_called()

    def test_failure_allows_retry_without_showing_exception_secrets(self):
        with patch.object(self.ui, '_generate', side_effect=RuntimeError('token=private')):
            self.panel.start()
            self.pump_until(lambda: not self.panel.running)
        self.assertIn('重试', self.panel.notice.get())
        self.assertNotIn('private', self.panel.notice.get())
        with patch.object(self.ui, '_generate', return_value=pathlib.Path(__file__)):
            self.panel.start()
            self.pump_until(lambda: not self.panel.running)
        self.assertIn('已生成', self.panel.notice.get())

    def test_custom_dates_validation_and_worker_range(self):
        self.panel.period.set('自定义日期')
        self.panel._date_visibility()
        self.panel.start_date.set('2026-09-09')
        self.panel.end_date.set('2026-09-07')
        with patch.object(self.ui,'_generate',return_value=pathlib.Path(__file__)) as generate:
            self.panel.start()
            self.assertFalse(self.panel.running)
            generate.assert_not_called()
            self.assertIn('开始日期',self.panel.notice.get())
            self.panel.end_date.set('2026-09-10')
            self.panel.start()
            self.pump_until(lambda:not self.panel.running)
            self.assertEqual(generate.call_args.args[1],('2026-09-09','2026-09-10'))

    def test_destroy_while_worker_pending_does_not_touch_tk(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(service, period, progress):
            entered.set()
            release.wait(2)
            progress('后台完成')
            done.set()
            return pathlib.Path(__file__)
        errors = []
        self.root.report_callback_exception = lambda *args: errors.append(args)
        with patch.object(self.ui, '_generate', generate):
            self.panel.start()
            self.assertTrue(entered.wait(1))
            self.panel.destroy()
            release.set()
            self.assertTrue(done.wait(1))
            self.root.update()
        self.assertEqual(errors, [])
        self.assertTrue(self.panel.closed)


if __name__ == '__main__':
    unittest.main()
