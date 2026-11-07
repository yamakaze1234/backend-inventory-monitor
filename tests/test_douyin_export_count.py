"""导出数量异步加载回归；虚拟时钟与离线 DOM 样例不提交真实报表。"""
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from douyin_export import ChromeExportWorker, EXPORT_COUNT_JS


class ExportCountTests(unittest.TestCase):
    def worker(self, values):
        worker = object.__new__(ChromeExportWorker)
        worker.expected_shop = '技嘉旗舰店'
        worker.open_page = Mock()
        worker.records = Mock(return_value=[])
        worker.check_shop = Mock()
        worker.click = Mock()
        iterator = iter(values)
        last = values[-1]
        def evaluate(_):
            nonlocal last
            last = next(iterator, last)
            return last
        worker.eval = Mock(side_effect=evaluate)
        self.clock = 0
        worker.pause = Mock(side_effect=lambda seconds: setattr(self, 'clock', self.clock + seconds))
        self.addCleanup(patch.stopall)
        patch('douyin_export.time.monotonic', side_effect=lambda: self.clock).start()
        return worker

    def test_loading_then_zero_then_correct_count_is_accepted(self):
        worker = self.worker([dict(count=None), dict(count='0'), dict(count='863')])
        self.assertEqual(worker.wait_export_count(863), 863)
        self.assertEqual(worker.pause.call_count, 2)

    def test_request_never_submits_until_modal_count_arrives(self):
        worker = self.worker([dict(tab='全部', count='863')] * 3 +
                             [dict(count=None), dict(count='863')])
        def click(selector, text):
            if text == '导出':
                self.assertGreaterEqual(worker.eval.call_count, 5)
        worker.click.side_effect = click
        result = worker.request_export()
        self.assertEqual(result['count'], 863)
        self.assertEqual(sum(c.args[1] == '导出' for c in worker.click.call_args_list), 1)

    def test_real_mismatch_never_submits_and_reports_both_counts(self):
        worker = self.worker([dict(tab='全部', count='863')] * 3 + [dict(count='862')])
        with self.assertRaisesRegex(ValueError, '查询 863 个，导出 862 个'):
            worker.request_export()
        self.assertFalse(any(c.args[1] == '导出' for c in worker.click.call_args_list))

    def test_missing_count_is_not_reported_as_a_real_mismatch(self):
        worker = self.worker([dict(count=None)])
        with self.assertRaisesRegex(ValueError, '数量尚未加载完成'):
            worker.wait_export_count(182, seconds=.5)

    def test_platform_limit_is_rejected_before_reading_or_submitting(self):
        worker = self.worker([dict(count='500000')])
        with self.assertRaisesRegex(ValueError, '平台导出上限'):
            worker.wait_export_count(500000)
        worker.eval.assert_not_called()
        worker.click.assert_not_called()

    def test_visible_drawer_only_and_ambiguous_or_loading_counts_are_rejected(self):
        # 执行生产 JavaScript；隐藏的旧窗口不能冒充当前窗口中的数量。
        script = r'''
const vm=require('node:vm');
const {expression, fixtures}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const values=fixtures.map(rows=>vm.runInNewContext(expression,{
  document:{querySelectorAll(selector){
    if(selector!=='.ecom-g-drawer-footer-help')throw new Error('未预期的选择器');
    return rows.map(row=>({...row,innerText:row.text,getClientRects(){return row.hidden?[]:[{}];}}));
  }},getComputedStyle:e=>({visibility:e.visibility||'visible'})
}));
process.stdout.write(JSON.stringify(values));
'''
        fixtures = [[dict(text='当前导出182个商品', hidden=True), dict(text='当前导出863个商品')],
                    [dict(text='当前导出个商品')],
                    [dict(text='当前导出863个商品'), dict(text='当前导出863个商品')],
                    [dict(text='当前导出182,345个商品')]]
        result = subprocess.run(['node', '-e', script],
            input=json.dumps(dict(expression=EXPORT_COUNT_JS, fixtures=fixtures)),
            capture_output=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout),
                         [dict(count='863'), dict(count=None), dict(count=None), dict(count='182345')])


if __name__ == '__main__':
    unittest.main()
