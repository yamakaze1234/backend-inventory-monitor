"""复现商品控件先出现、店名延迟加载的登录校验竞态；不连接真实店铺。"""
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from douyin_export import PRODUCT_URL
from douyin_managed_export import ManagedExportWorker


class LoginReadinessTests(unittest.TestCase):
    def worker(self, states):
        worker = ManagedExportWorker(None, Path('.') / 'profiles' / ('a' * 32))
        worker.expected_shop = '技嘉旗舰店'
        worker.expected_browser = 'chrome'
        worker.stopped = threading.Event()
        worker._browser = Mock()
        worker.pause = Mock()
        pending = iter(states)
        current = states[-1]

        def evaluate(expression):
            nonlocal current
            current = next(pending, current)
            # 用真正的 JavaScript 执行生产等待条件，避免只模拟“等待成功”。
            script = r'''
const vm = require('node:vm');
const {expression, state} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const context = {
  location: {href: state.url || 'https://fxg.jinritemai.com/ffa/g/list'},
  navigator: {userAgent: 'Chrome/1'},
  document: {readyState: state.ready || 'interactive', querySelector(selector) {
    if (selector === '#exportSearchedGoods') return state.button === false ? null : {};
    if (selector === '[class*="index_userName"]') {
      return state.shop == null ? null : {textContent: state.shop};
    }
    throw new Error('未预期的测试选择器');
  }}
};
process.stdout.write(JSON.stringify(vm.runInNewContext(expression, context)));
'''
            result = subprocess.run(['node', '-e', script],
                input=json.dumps(dict(expression=expression, state=current)),
                capture_output=True, text=True, encoding='utf-8', check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return json.loads(result.stdout)

        worker.eval = Mock(side_effect=evaluate)
        return worker

    def test_button_before_shop_waits_for_nonempty_identity(self):
        # 导出按钮就绪时，缺失和空白店名均继续等；完整同店名称才允许校验。
        worker = self.worker([{}, dict(shop='  '), dict(shop='技嘉旗舰店')])
        worker.open_page(PRODUCT_URL, '#exportSearchedGoods')
        self.assertEqual(worker.pause.call_count, 2)
        worker._browser.navigate.assert_called_once_with(PRODUCT_URL)

    def test_loaded_wrong_shop_is_rejected_without_waiting_for_a_match(self):
        worker = self.worker([dict(shop='另一家店铺')])
        with self.assertRaisesRegex(ValueError, '店铺与配置不一致'):
            worker.open_page(PRODUCT_URL, '#exportSearchedGoods')
        worker.pause.assert_not_called()

    def test_missing_identity_times_out_instead_of_accepting_the_page(self):
        worker = self.worker([{}])
        with patch('douyin_managed_export.time.monotonic', side_effect=[0, 0, 21]):
            with self.assertRaisesRegex(ValueError, '页面未就绪或登录失效'):
                worker.open_page(PRODUCT_URL, '#exportSearchedGoods')
        self.assertEqual(worker.eval.call_count, 1)


if __name__ == '__main__':
    unittest.main()
