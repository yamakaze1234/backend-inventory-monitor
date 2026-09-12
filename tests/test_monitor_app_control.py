import unittest
from unittest.mock import patch, MagicMock
from monitor_app_control import summarize_status
from source_monitor_supervisor import Supervisor


class Worker:
    def __init__(self, source):
        self.alive = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.alive = True

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stops += 1
        self.alive = False


class MonitorControlTests(unittest.TestCase):
    def test_start_only_enables_after_worker_upgrade_succeeds(self):
        import monitor_app_control as module
        controller = module.Controller({'workspace': '.', 'portable': True})
        order = []
        with patch.object(controller, 'ensure_current_worker', side_effect=lambda: order.append('upgrade')), \
                patch.object(controller, 'set_enabled', side_effect=lambda enabled: order.append(enabled)):
            controller.start()
        self.assertEqual(order, ['upgrade', True])
        with patch.object(controller, 'ensure_current_worker', side_effect=RuntimeError('upgrade failed')), \
                patch.object(controller, 'set_enabled') as enable:
            with self.assertRaises(RuntimeError):
                controller.start()
        enable.assert_not_called()

    def test_current_worker_is_not_restarted(self):
        import monitor_app_control as module
        controller = module.Controller({'workspace': '.', 'portable': True})
        snapshot = {'pid': 4, 'heartbeat': 100, 'robot_protocol': 1, 'enabled': True}
        with patch.object(module, 'read_json', return_value=snapshot), patch.object(module.time, 'time', return_value=100), \
                patch.object(module, 'is_process_alive', return_value=True), patch.object(controller, 'stop') as stop, \
                patch.object(controller, 'ensure_task') as launch:
            controller.ensure_current_worker()
        stop.assert_not_called()
        launch.assert_not_called()

    def test_absent_worker_is_started(self):
        import monitor_app_control as module
        controller = module.Controller({'workspace': '.', 'portable': True})
        with patch.object(module, 'read_json', return_value={}), patch.object(controller, 'ensure_task') as launch:
            controller.ensure_current_worker()
        launch.assert_called_once()

    def test_legacy_worker_waits_for_fresh_pause_and_dead_children(self):
        import monitor_app_control as module
        controller = module.Controller({'workspace': '.', 'portable': True})
        legacy = {'pid': 4, 'heartbeat': 99, 'enabled': True, 'sources': []}
        stale = dict(legacy, heartbeat=99, enabled=False)
        live = dict(legacy, heartbeat=101, enabled=False, sources=[{'pid': 5}])
        paused = dict(legacy, heartbeat=102, enabled=False)
        order = []
        def terminate(pid, check):
            self.assertEqual(pid, 4)
            self.assertTrue(check())
            order.append('terminate')
        with patch.object(module, 'read_json', side_effect=[legacy, stale, live, paused, paused]), \
                patch.object(module.time, 'time', return_value=103), patch.object(module.time, 'sleep'), \
                patch.object(module, 'is_process_alive', return_value=True), \
                patch.object(controller, 'stop', return_value=100) as stop, \
                patch.object(module, 'terminate_paused_worker', side_effect=terminate) as kill, \
                patch.object(controller, 'ensure_task', side_effect=lambda: order.append('launch')):
            controller.ensure_current_worker()
        stop.assert_called_once()
        kill.assert_called_once()
        self.assertEqual(order, ['terminate', 'launch'])

    def test_pause_timeout_never_terminates_or_launches(self):
        import monitor_app_control as module
        controller = module.Controller({'workspace': '.', 'portable': True})
        snapshot = {'pid': 4, 'heartbeat': 100, 'enabled': True}
        with patch.object(module, 'read_json', return_value=snapshot), patch.object(module.time, 'time', return_value=100), \
                patch.object(module, 'is_process_alive', return_value=True), patch.object(controller, 'stop', return_value=100), \
                patch.object(module.time, 'monotonic', side_effect=[0, 12]), \
                patch.object(module, 'terminate_paused_worker') as kill, patch.object(controller, 'ensure_task') as launch:
            with self.assertRaisesRegex(RuntimeError, '退出旧版程序或重启电脑'):
                controller.ensure_current_worker()
        kill.assert_not_called()
        launch.assert_not_called()

    def test_verified_image_and_termination_use_same_handle(self):
        import monitor_app_control as module
        kernel = MagicMock()
        kernel.OpenProcess.return_value = 123
        def image(handle, flags, buffer, length):
            buffer.value = r'C:\\Apps\\后台库存监控.exe'
            return True
        kernel.QueryFullProcessImageNameW.side_effect = image
        kernel.TerminateProcess.return_value = True
        kernel.WaitForSingleObject.return_value = 0
        with patch.object(module.ctypes, 'WinDLL', return_value=kernel):
            module.terminate_paused_worker(4, lambda: True)
        self.assertEqual(kernel.QueryFullProcessImageNameW.call_args.args[0], 123)
        kernel.TerminateProcess.assert_called_once_with(123, 0)
        kernel.WaitForSingleObject.assert_called_once_with(123, 3000)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_unrecognized_image_or_lost_pause_is_never_terminated(self):
        import monitor_app_control as module
        for path, paused in [(r'C:\\Apps\\other.exe', True), (r'C:\\Apps\\钉钉货源监控.exe', False)]:
            with self.subTest(path=path, paused=paused):
                kernel = MagicMock()
                kernel.OpenProcess.return_value = 123
                def image(handle, flags, buffer, length):
                    buffer.value = path
                    return True
                kernel.QueryFullProcessImageNameW.side_effect = image
                with patch.object(module.ctypes, 'WinDLL', return_value=kernel):
                    with self.assertRaises(RuntimeError):
                        module.terminate_paused_worker(4, lambda: paused)
                kernel.TerminateProcess.assert_not_called()
                kernel.CloseHandle.assert_called_once_with(123)

    def test_stale_supervisor_does_not_hide_live_children_on_exit(self):
        import monitor_app_control as module
        snapshot = {'heartbeat': 1, 'pid': 1, 'enabled': True,
                    'sources': [{'pid': 2, 'status': 'ready'}]}
        controller = module.Controller({'workspace': '.', 'powershell': 'pwsh.exe'})
        with patch.object(module, 'read_json', side_effect=[snapshot, {'enabled': False}]), \
                patch.object(module, 'is_process_alive', return_value=True):
            result = controller.status()
        self.assertEqual(result['state'], 'stopping')
        self.assertTrue(result['sources'][0]['live'])
        self.assertEqual(result['ready'], 0)

    def test_missing_supervisor_and_children_confirm_stopped(self):
        import monitor_app_control as module
        controller = module.Controller({'workspace': '.', 'powershell': 'pwsh.exe'})
        with patch.object(module, 'read_json', side_effect=[{}, {'enabled': False}]), \
                patch.object(module, 'is_process_alive', return_value=False):
            self.assertEqual(controller.status()['state'], 'paused')

    def test_pause_is_not_undone_by_dingtalk_running(self):
        manager = Supervisor([{}], Worker)
        manager.tick(True)
        manager.tick(True, enabled=False)
        manager.tick(True, enabled=False)
        self.assertFalse(manager.workers[0].alive)
        self.assertEqual(manager.workers[0].starts, 1)
        manager.tick(True, enabled=True)
        self.assertEqual(manager.workers[0].starts, 2)

    def test_manual_start_can_start_without_desktop_client(self):
        manager = Supervisor([{}], Worker)
        manager.tick(False, force_start=True)
        self.assertTrue(manager.workers[0].alive)

    def test_stale_ready_is_not_reported_as_running(self):
        snapshot = {'heartbeat': 10, 'pid': 1, 'enabled': True,
                    'sources': [{'pid': 2, 'status': 'ready'}]}
        self.assertEqual(summarize_status(snapshot, now=100, alive=lambda _: True)['state'], 'offline')

    def test_dead_child_is_not_reported_as_ready(self):
        snapshot = {'heartbeat': 99, 'pid': 1, 'enabled': True,
                    'sources': [{'pid': 2, 'status': 'ready'}]}
        result = summarize_status(snapshot, now=100, alive=lambda pid: pid == 1)
        self.assertEqual(result['ready'], 0)
        self.assertEqual(result['state'], 'connecting')

    def test_live_ready_and_persisted_pause_are_distinct(self):
        snapshot = {'heartbeat': 99, 'pid': 1, 'enabled': True,
                    'sources': [{'pid': 2, 'status': 'ready'}]}
        self.assertEqual(summarize_status(snapshot, now=100, alive=lambda _: True)['state'], 'running')
        snapshot['enabled'] = False
        snapshot['sources'][0]['status'] = 'paused'
        snapshot['sources'][0]['pid'] = None
        self.assertEqual(summarize_status(snapshot, now=100, alive=lambda _: True)['state'], 'paused')


if __name__ == '__main__':
    unittest.main()
