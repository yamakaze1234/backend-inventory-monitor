"""更新验收使用临时签名密钥与内存下载，不访问真实发布通道。"""
import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from inventory_updater import InventoryUpdater, verify_manifest, version_key, restart_arguments


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        output=Path(__file__).resolve().parents[1]/'outputs/订单整合验收/tests'
        output.mkdir(parents=True,exist_ok=True)
        self.temp=Path(tempfile.mkdtemp(dir=output))
        self.key=Ed25519PrivateKey.generate()
        self.public=base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
        self.binary=b'MZ'+b'isolated-test-binary'*20
        self.manifest=dict(app='backend-inventory-monitor',platform='windows-x86_64',version='1.07',
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

    def test_restart_preserves_workspace_but_not_worker_or_old_receipt(self):
        args=restart_arguments(['--data-dir',str(self.temp/'中文 工作区'),'--tray','--worker','--update-health','old.txt'])
        self.assertEqual(['--data-dir',str((self.temp/'中文 工作区').resolve()),'--tray'],args)
        self.assertEqual(['--data-dir',str((self.temp/'中文 工作区').resolve())],
                         restart_arguments(['--data-dir='+str(self.temp/'中文 工作区')]))


if __name__=='__main__':unittest.main()
