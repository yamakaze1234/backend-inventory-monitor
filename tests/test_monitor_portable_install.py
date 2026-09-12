import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock


class InstallTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('monitor_portable_install'), '安装模块尚未实现')
        import monitor_portable_install
        return monitor_portable_install

    def test_dry_run_has_no_side_effects(self):
        m = self.module()
        with patch.dict(os.environ, {'LOCALAPPDATA': r'C:\用户 空格\Local'}), patch.object(m, '_desktop_dir', return_value=Path(r'C:\桌面')), patch.object(Path, 'mkdir') as mkdir, patch.object(m.shutil, 'copy2') as copy, patch.object(m, '_set_autostart') as reg, patch.object(m, '_create_shortcut') as shortcut:
            result = m.install_current_user(source_executable='source.exe', dry_run=True)
            self.assertIn('app-2.0.0', result['executable'])
            self.assertFalse(result['autostart'])
            for mock in (mkdir, copy, reg, shortcut):
                mock.assert_not_called()

    def test_frozen_worker_arguments(self):
        m = self.module()
        with patch.object(m.sys, 'frozen', True, create=True), patch.object(m.sys, 'executable', r'C:\路径 空格\程序.exe'), patch.object(m.subprocess, 'Popen') as popen:
            m.launch_worker(Path(r'C:\数据 空格'))
            self.assertEqual(popen.call_args.args[0], [r'C:\路径 空格\程序.exe', '--worker', '--data-dir', r'C:\数据 空格'])
            self.assertFalse(popen.call_args.kwargs.get('shell', False))

    def test_frozen_worker_has_independent_extraction_lifetime(self):
        m = self.module()
        with patch.object(m.sys, 'frozen', True, create=True), patch.dict(os.environ, {'PYINSTALLER_RESET_ENVIRONMENT': '0'}), patch.object(m.subprocess, 'Popen') as popen:
            m.launch_worker('data')
            self.assertEqual(popen.call_args.kwargs.get('env', {}).get('PYINSTALLER_RESET_ENVIRONMENT'), '1')
            self.assertEqual(os.environ['PYINSTALLER_RESET_ENVIRONMENT'], '0')

    def test_resource_prefers_bundled_asset(self):
        m = self.module()
        with patch.object(m.sys, '_MEIPASS', r'C:\解压 目录', create=True):
            self.assertEqual(m.resource_path('dws.exe'), Path(r'C:\解压 目录') / 'dws.exe')
        with patch.object(m, 'resource_path', return_value=Path('dws.exe')), patch.object(Path, 'is_file', return_value=False), patch.object(m.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'DWS'):
                m.bundled_dws()

    def test_idempotent_install_does_not_copy_self(self):
        m = self.module()
        # Retain the test directory; project policy forbids forced deletion.
        root = Path(tempfile.mkdtemp(prefix='monitor-install-test-'))
        with patch.dict(os.environ, {'LOCALAPPDATA': str(root)}), patch.object(m, '_desktop_dir', return_value=root / '桌面'), patch.object(m, '_set_autostart') as reg, patch.object(m, '_create_shortcut') as shortcut, patch.object(m.shutil, 'copy2') as copy:
            target = root / 'DingTalkSourceMonitor' / 'app-2.0.0' / '钉钉货源监控.exe'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'test executable')
            result = m.install_current_user(source_executable=target)
            copy.assert_not_called()
            self.assertTrue(result['autostart'])
            reg.assert_called_once()
            shortcut.assert_called_once()

    def test_registry_command_quotes_unicode_executable(self):
        m = self.module()
        registry = MagicMock()
        with patch.dict('sys.modules', {'winreg': registry}):
            m._set_autostart(Path(r'C:\程序 空格\监控.exe'), Path(r'C:\数据 空格'))
        self.assertEqual(registry.SetValueEx.call_args.args[4], '"C:\\程序 空格\\监控.exe" --tray --data-dir "C:\\数据 空格"')

    def test_shortcut_uses_static_script_and_argument_array(self):
        m = self.module()
        root = Path(tempfile.mkdtemp(prefix='monitor-shortcut-test-'))
        exe, shortcut, data = root / '程序 空格.exe', root / '监控.lnk', root / '数据 空格'
        with patch.object(m.subprocess, 'run', return_value=MagicMock(returncode=0)) as run, patch.object(Path, 'is_file', return_value=True):
            m._create_shortcut(shortcut, exe, data)
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], [str(shortcut), str(exe), m.subprocess.list2cmdline(['--data-dir', str(data)]), str(root)])
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertNotIn(str(data), (root / 'create_shortcut.vbs').read_text())

    def test_source_worker_uses_pythonw_if_available(self):
        m = self.module()
        with patch.object(m.sys, 'frozen', False, create=True), patch.object(m.sys, 'executable', r'C:\Python\python.exe'), patch.object(Path, 'is_file', return_value=True), patch.object(m.subprocess, 'Popen') as popen:
            m.launch_worker('data')
        self.assertEqual(popen.call_args.args[0][0], r'C:\Python\pythonw.exe')
        self.assertTrue(popen.call_args.args[0][1].endswith('monitor_tray_app.py'))


if __name__ == '__main__':
    unittest.main()
