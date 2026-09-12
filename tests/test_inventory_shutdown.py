import unittest
from unittest.mock import Mock, patch


class ShutdownTests(unittest.TestCase):
    def test_stop_is_requested_before_exit_is_allowed(self):
        from inventory_app import stop_dingtalk_and_wait
        controller = Mock()
        controller.status.side_effect = [dict(state='stopping', sources=[dict(live=True)]),
                                         dict(state='paused', sources=[])]
        with patch('inventory_app.time.sleep'):
            stop_dingtalk_and_wait(controller)
        self.assertEqual(controller.mock_calls[0], unittest.mock.call.stop())
        self.assertEqual(controller.status.call_count, 2)

    def test_live_child_prevents_exit_even_with_paused_label(self):
        from inventory_app import stop_dingtalk_and_wait
        controller = Mock()
        controller.status.return_value = dict(state='paused', sources=[dict(live=True)])
        with patch('inventory_app.time.monotonic', side_effect=[0, 0, 20]), patch('inventory_app.time.sleep'):
            with self.assertRaises(TimeoutError):
                stop_dingtalk_and_wait(controller)

    def test_failed_stop_propagates_without_claiming_success(self):
        from inventory_app import stop_dingtalk_and_wait
        controller = Mock()
        controller.stop.side_effect = OSError('fixture')
        with self.assertRaises(OSError):
            stop_dingtalk_and_wait(controller)
        controller.status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
