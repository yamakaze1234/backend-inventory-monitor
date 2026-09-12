import importlib.util
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch


class PreviewUITests(unittest.TestCase):
    def test_preview_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('inventory_report_preview'))

    def setUp(self):
        if importlib.util.find_spec('inventory_report_preview') is None:
            return
        import inventory_report_preview as ui
        self.ui = ui
        self.root = tk.Tk(); self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'report.md'
        self.path.write_text('报告全文\n第二行', encoding='utf-8')
        self.targets = [dict(id='a',name='一号',enabled=True,revision='1'),
                        dict(id='b',name='暂停号',enabled=False,revision='2')]
        self.store_patch = patch.object(ui, 'store_for_service', return_value=object())
        self.store_patch.start(); self.addCleanup(self.store_patch.stop)
        self.dest_patch = patch.object(ui, 'destinations', return_value=self.targets)
        self.dest_patch.start(); self.addCleanup(self.dest_patch.stop)
        self.sender_patch = patch.object(ui, 'send_reviewed')
        self.sender = self.sender_patch.start(); self.addCleanup(self.sender_patch.stop)
        self.window = ui.open_preview(self.root, object(), self.path)
        self.root.update()

    def require_ui(self):
        if not hasattr(self, 'ui'): self.skipTest('module not yet implemented')

    def test_open_is_read_only_and_targets_default_empty(self):
        self.require_ui()
        self.assertEqual(self.window.body(), '报告全文\n第二行')
        self.assertFalse(any(v.get() for v in self.window.target_vars.values()))
        self.assertIn('disabled', self.window.target_checks['b'].state())
        self.sender.assert_not_called()

    def test_changes_clear_review_and_local_save_does_not_send(self):
        self.require_ui(); w = self.window
        w.target_vars['a'].set(True); w.reviewed.set(True)
        w.editor.insert('end', '修改'); self.root.update()
        self.assertFalse(w.reviewed.get())
        w.reviewed.set(True); w.target_vars['a'].set(False)
        self.assertFalse(w.reviewed.get())
        w.save_local()
        self.assertEqual(self.path.read_text('utf-8'), w.body())
        self.sender.assert_not_called()

    def test_cancel_confirmation_never_sends(self):
        self.require_ui(); w = self.window
        w.target_vars['a'].set(True); w.reviewed.set(True)
        with patch.object(self.ui.messagebox, 'askyesno', return_value=False):
            w.confirm_send()
        self.sender.assert_not_called()
        self.assertFalse(w.busy)

    def test_validation_error_is_not_sent(self):
        self.require_ui(); w = self.window
        self.sender.side_effect = ValueError('机器人配置已变化，请重新选择')
        w.target_vars['a'].set(True); w.reviewed.set(True)
        with patch.object(self.ui.messagebox, 'askyesno', return_value=True):
            w.confirm_send()
        deadline = time.monotonic() + 3
        while w.busy and time.monotonic() < deadline:
            self.root.update(); time.sleep(.02)
        self.assertIn('未发送', w.result_var.get())
        self.assertIn('配置已变化', w.result_var.get())
        self.assertNotIn('结果待核实', w.result_var.get())

    def test_thread_start_failure_restores_editing(self):
        self.require_ui(); w = self.window
        w.target_vars['a'].set(True); w.reviewed.set(True)
        with patch.object(self.ui.messagebox, 'askyesno', return_value=True), patch.object(self.ui.threading.Thread, 'start', side_effect=RuntimeError()):
            w.confirm_send()
        self.assertFalse(w.busy)
        self.assertEqual(w.editor.cget('state'), 'normal')
        self.assertIn('未发送', w.result_var.get())
        self.sender.assert_not_called()

    def test_confirmation_worker_lock_and_results(self):
        self.require_ui(); w = self.window
        gate = threading.Event(); self.addCleanup(gate.set)
        def send(*args):
            self.assertNotEqual(threading.current_thread(), threading.main_thread())
            gate.wait(2)
            return [dict(id='a',name='一号',status='unknown',sent_parts=0,total_parts=1)]
        self.sender.side_effect = send
        w.target_vars['a'].set(True); w.reviewed.set(True)
        with patch.object(self.ui.messagebox, 'askyesno', return_value=True) as confirm:
            w.confirm_send()
        self.assertIn('一号', confirm.call_args.args[1])
        self.assertIn('1 段', confirm.call_args.args[1])
        self.assertEqual(w.editor.cget('state'), 'disabled')
        with patch.object(self.ui.messagebox, 'showinfo'):
            w.close_preview()
        self.assertTrue(w.winfo_exists())
        gate.set()
        deadline = time.monotonic() + 3
        while w.busy and time.monotonic() < deadline:
            self.root.update(); time.sleep(.02)
        self.assertFalse(w.busy)
        self.assertIn('结果待核实', w.result_var.get())
        self.assertIn('不自动重试', w.result_var.get())
        self.assertFalse(w.reviewed.get())
        self.assertEqual(self.sender.call_count, 1)


if __name__ == '__main__': unittest.main()
