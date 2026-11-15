"""更新验收使用临时签名密钥与内存下载，不访问真实发布通道。"""
import base64
import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from inventory_updater import InventoryUpdater, verify_manifest, version_key, restart_arguments, powershell_executable


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        output=Path(__file__).resolve().parents[1]/'outputs/订单整合验收/tests'
        output.mkdir(parents=True,exist_ok=True)
        self.temp=Path(tempfile.mkdtemp(dir=output))
        self.key=Ed25519PrivateKey.generate()
        self.public=base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
        self.binary=b'MZ'+b'isolated-test-binary'*20
        self.manifest=dict(app='backend-inventory-monitor',platform='windows-x86_64',version='99.0',
            sha256=hashlib.sha256(self.binary).hexdigest(),size=len(self.binary),notes='隔离测试',
            urls=['https://gitea.cq360.top/CoreStudio/backend-inventory-monitor/releases/download/v1.07/test.exe'])

    # 测试目录在验收结束后统一回收，不直接永久删除。

    def envelope(self):
        payload=json.dumps(self.manifest).encode()
        return dict(payload=base64.b64encode(payload).decode(),signature=base64.b64encode(self.key.sign(payload)).decode())

    def test_manifest_rejects_tamper_wrong_app_and_foreign_url(self):
        item=self.envelope();item['payload']=base64.b64encode(b'{}').decode()
        with self.assertRaises(ValueError):verify_manifest(item,self.public)
        self.manifest['app']='other'
        with self.assertRaises(ValueError):verify_manifest(self.envelope(),self.public)
        self.manifest['app']='backend-inventory-monitor'
        self.manifest['urls']=['https://example.com/test.exe']
        with self.assertRaises(ValueError):verify_manifest(self.envelope(),self.public)

    def test_primary_failure_falls_back_and_binary_matches(self):
        calls=[]
        def opener(request,timeout):
            calls.append(request.full_url)
            if len(calls)==1:raise OSError('主源失败')
            return io.BytesIO(json.dumps(self.envelope()).encode() if request.full_url.endswith('.json') else self.binary)
        updater=InventoryUpdater(self.temp,self.public,opener)
        self.assertTrue(updater.check()['available'])
        self.assertEqual('downloaded',updater.download()['phase'])
        self.assertEqual(self.binary,updater.staged.read_bytes())
        self.assertEqual(3,len(calls))

    def test_truncated_and_oversized_downloads_never_install(self):
        for content in (self.binary[:-1],self.binary+b'extra'):
            def opener(request,timeout):
                return io.BytesIO(json.dumps(self.envelope()).encode() if request.full_url.endswith('.json') else content)
            updater=InventoryUpdater(self.temp,self.public,opener)
            updater.check()
            with self.assertRaises(ValueError):updater.download()
            self.assertIsNone(updater.staged)
            self.assertEqual('error',updater.state['phase'])

    def test_install_busy_and_source_mode_rejected(self):
        updater=InventoryUpdater(self.temp,self.public)
        with self.assertRaisesRegex(ValueError,'匹配任务'):updater.install(busy=True)
        with self.assertRaisesRegex(ValueError,'源码运行'):updater.install()

    def test_numeric_version_comparison(self):
        self.assertGreater(version_key('1.10'),version_key('1.06'))
        self.assertEqual(version_key('1.06'),version_key('1.6.0'))
        for value in ('1.7-rc1','latest','1.2.exe',None):
            with self.assertRaises(ValueError):version_key(value)

    def test_manifest_rejects_boolean_size_and_string_urls(self):
        # JSON 布尔值不能充当文件大小，地址必须是列表。
        self.manifest['size'] = True
        with self.assertRaises(ValueError):verify_manifest(self.envelope(),self.public)
        self.manifest['size'] = len(self.binary)
        self.manifest['urls'] = self.manifest['urls'][0]
        with self.assertRaises(ValueError):verify_manifest(self.envelope(),self.public)

    def test_shell_finds_new_install_before_path_refresh(self):
        # 部署完成但当前进程 PATH 尚未刷新时仍可更新。
        with patch('inventory_updater.shutil.which',return_value=None), patch('inventory_updater.Path.is_file',return_value=True), patch.dict('os.environ',{'ProgramFiles':str(self.temp)}):
            self.assertEqual(str(self.temp/'PowerShell/7/pwsh.exe'),powershell_executable())
        with patch('inventory_updater.shutil.which',return_value=None), patch('inventory_updater.Path.is_file',return_value=False):
            with self.assertRaisesRegex(ValueError,'一键部署环境'):powershell_executable()

    def test_helper_launch_failure_does_not_report_installing(self):
        # 模拟助手被系统阻止，必须保留主程序而非报告已接管更新。
        updater=InventoryUpdater(self.temp,self.public)
        updater.root.mkdir(parents=True,exist_ok=True)
        updater.staged=updater.root/'candidate.exe'
        updater.staged.write_bytes(self.binary)
        updater.release=self.manifest
        updater.state['phase']='downloaded'
        with patch('inventory_updater.sys.frozen',True,create=True), patch('inventory_updater.sys.argv',['app.exe','--tray']), patch('inventory_updater.powershell_executable',return_value='pwsh.exe'), patch('inventory_updater.subprocess.Popen') as launch:
            launch.return_value.poll.return_value=1
            with self.assertRaisesRegex(ValueError,'更新助手未就绪'):updater.install()
            self.assertNotEqual('installing',updater.state['phase'])
            self.assertIn('Bypass',launch.call_args.args[0])
            # 双击 EXE 无数据目录参数，安装计划仍绑定已打开的实际目录。
            plan=json.loads((updater.root/'install.json').read_text(encoding='utf-8'))
            self.assertEqual(plan['arguments'], ['--data-dir',str(self.temp.resolve()),'--tray'])

    def test_restart_preserves_workspace_but_not_worker_or_old_receipt(self):
        args=restart_arguments(['--data-dir',str(self.temp/'中文 工作区'),'--tray','--worker','--update-health','old.txt'])
        self.assertEqual(['--data-dir',str((self.temp/'中文 工作区').resolve()),'--tray'],args)
        self.assertEqual(['--data-dir',str((self.temp/'中文 工作区').resolve())],
                         restart_arguments(['--data-dir='+str(self.temp/'中文 工作区')]))


if __name__=='__main__':unittest.main()
