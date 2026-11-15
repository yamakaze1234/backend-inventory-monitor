"""CWS 下载归属与截止时间，不连接真实浏览器。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from douyin_cws_export import CwsExportWorker


class CwsDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = Mock(downloads_dir=self.root / 'downloads')
        self.page_session = self.script.page.context.new_cdp_session.return_value
        self.page_session.send.side_effect = lambda method: ({'frameTree': {'frame': {'id': 'our-frame'}}}
            if method == 'Page.getFrameTree' else {'targetInfo': {'browserContextId': 'our-context'}})
        self.session = self.script.page.context.browser.new_browser_cdp_session.return_value
        self.handlers = {}
        self.session.on.side_effect = lambda event, handler: self.handlers.update({event: handler})
        self.worker = CwsExportWorker(self.script, self.root / 'state', '技嘉旗舰店')

    def test_download_only_claims_our_page_and_uses_cws_directory(self):
        def trigger():
            self.handlers['Browser.downloadWillBegin'](dict(frameId='other-frame', guid='other', suggestedFilename='other.xlsx'))
            self.handlers['Browser.downloadProgress'](dict(guid='other', state='completed'))
            self.handlers['Browser.downloadWillBegin'](dict(frameId='our-frame', guid='our-guid', suggestedFilename='productExport.xlsx'))
            (self.script.downloads_dir / 'our-guid').write_bytes(b'xlsx fixture')
            self.handlers['Browser.downloadProgress'](dict(guid='our-guid', state='completed'))
        target = self.worker._download(trigger)
        self.assertEqual(target, self.script.downloads_dir / 'our-guid.xlsx')
        self.assertTrue(target.is_file())
        self.assertEqual(self.session.send.call_args.args[1]['browserContextId'], 'our-context')
        self.session.detach.assert_called_once()

    def test_timeout_cancels_only_owned_download_and_detaches(self):
        def trigger():
            self.handlers['Browser.downloadWillBegin'](dict(frameId='our-frame', guid='our-guid', suggestedFilename='productExport.xlsx'))
        with patch('douyin_cws_export.time.monotonic', side_effect=[0, 61]):
            with self.assertRaisesRegex(ValueError, '下载超时'):
                self.worker._download(trigger)
        self.session.send.assert_any_call('Browser.cancelDownload', dict(guid='our-guid', browserContextId='our-context'))
        self.session.detach.assert_called_once()
        self.page_session.detach.assert_called_once()
