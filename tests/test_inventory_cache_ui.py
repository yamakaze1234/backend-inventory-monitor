"""Offline cleanup UI checks; all backend operations are fakes."""
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch


class CacheUITests(unittest.TestCase):
    def setUp(self):
        import inventory_cache_ui as ui
        self.ui = ui
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.panel = ui.build_section(self.root, object())
        self.plan = dict(days=30, cutoff='2026-08-12', groups=[dict(label='消息缓存', rows=4, bytes=100)], files=[dict(path='D:/fixture/cache.txt', size=200)], row_count=4, file_count=1, total_bytes=300)

    def pump(self):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            self.root.update()
            if not self.panel.running:
                return
            time.sleep(.01)
        self.fail('worker timeout')

    def preview(self):
        with patch.object(self.ui, '_preview', return_value=self.plan):
            self.panel.preview()
            self.pump()

    def test_preview_and_changed_range_invalidates(self):
        self.assertEqual(self.panel.days.get(), '30天前')
        self.preview()
        self.assertEqual(str(self.panel.clean_button['state']), 'normal')
        text = self.panel.details.get('1.0', 'end')
        self.assertIn('消息缓存', text)
        self.assertIn('D:/fixture/cache.txt', text)
        self.panel.days.set('7天前')
        self.assertIsNone(self.panel.plan)
        self.assertEqual(str(self.panel.clean_button['state']), 'disabled')

    def test_confirm_cancel_then_worker_once_and_partial_result(self):
        self.preview()
        with patch.object(self.ui.messagebox, 'askyesno', return_value=False), patch.object(self.ui, '_execute') as execute:
            self.panel.clean()
            execute.assert_not_called()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        main = threading.get_ident()
        def execute(service, plan):
            self.assertNotEqual(main, threading.get_ident())
            entered.set()
            release.wait(2)
            return dict(deleted_rows=2, recycled_files=0, skipped=2, errors=['文件正在使用，已保留。'])
        with patch.object(self.ui.messagebox, 'askyesno', return_value=True) as confirm, patch.object(self.ui, '_execute', side_effect=execute) as call:
            self.panel.clean()
            self.assertTrue(entered.wait(1))
            self.panel.clean()
            release.set()
            self.pump()
            self.assertEqual(call.call_count, 1)
            self.assertIn('30天前', confirm.call_args.args[1])
            self.assertIn('失败', self.panel.notice.get())
            self.assertNotIn('成功', self.panel.notice.get())
            self.assertIsNone(self.panel.plan)

    def test_empty_and_failure_do_not_enable_cleanup_or_expose_exception(self):
        self.plan.update(row_count=0, file_count=0)
        self.preview()
        self.assertEqual(str(self.panel.clean_button['state']), 'disabled')
        with patch.object(self.ui, '_preview', side_effect=RuntimeError('token=secret')):
            self.panel.preview()
            self.pump()
        self.assertNotIn('secret', self.panel.notice.get())

    def test_all_range_preview_runs_off_main_thread(self):
        main = threading.get_ident()
        def preview(service, days):
            self.assertEqual(days, 0)
            self.assertNotEqual(main, threading.get_ident())
            return self.plan
        self.panel.days.set('全部')
        with patch.object(self.ui, '_preview', side_effect=preview):
            self.panel.preview()
            self.pump()
        self.assertIsNotNone(self.panel.plan)

    def test_destroy_during_worker(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def preview(*args):
            entered.set()
            release.wait(2)
            done.set()
            return self.plan
        errors = []
        self.root.report_callback_exception = lambda *args: errors.append(args)
        with patch.object(self.ui, '_preview', side_effect=preview):
            self.panel.preview()
            self.assertTrue(entered.wait(1))
            self.panel.destroy()
            release.set()
            self.assertTrue(done.wait(1))
            self.root.update()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
