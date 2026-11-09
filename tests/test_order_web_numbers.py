"""用隔离订单验证网店单号匹配和通知，避免访问真实 ERP 和群。"""
import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


class OrderWebNumberTests(unittest.TestCase):
    first = '100123456789012345A-1'
    second = '200123456789012345A'
    internal = 'NO-INTERNAL-001'

    def setUp(self):
        # 只加载随软件部署的机器人兼容层，替换外部 ERP 和部门配置依赖。
        stubs = {
            'patrol_service': dict(single_instance=None),
            'patrol_worker': dict(BatchMatchAdapter=object, PatrolWorker=None),
            'task_store': dict(TaskStore=None),
            'robot_config': dict(get_config=None),
            'order_department_runtime': dict(settings=lambda: {
                'state_root': '.', 'dws_path': 'fixture', 'shops': [{'id': '1', 'name': '样例店'}]}),
        }
        modules = {name: types.SimpleNamespace(**members) for name, members in stubs.items()}
        path = Path(__file__).resolve().parents[1] / 'integrations/order_backend/matching/group_order_bot.py'
        spec = importlib.util.spec_from_file_location('fixture_group_order_bot', path)
        self.bot = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.bot)

    def test_find_orders_prefers_web_number_preserves_suffix_and_internal_identity(self):
        row = dict(shopId='1', billcode=self.internal, platformNo=f'{self.first}， {self.second}')
        adapter = Mock()
        adapter.gateway.orders.return_value = ([row], 1)
        adapter.__enter__ = Mock(return_value=adapter)
        adapter.__exit__ = Mock(return_value=False)
        gate = Mock()
        with patch.dict(sys.modules, batch_match=types.SimpleNamespace(order_gate=gate)):
            found = self.bot.find_orders(self.second, ['1'], None, None, lambda: adapter)
            adapter.gateway.orders.assert_called_once_with('1', None, None, self.second, 0)
            gate.assert_called_once_with(row)
        self.assertEqual(found['1'][0]['billcode'], self.internal)
        self.assertEqual(self.bot.resolved_web_order_numbers(found), [self.first, self.second])
        self.assertTrue(self.bot.order_number_matches(self.internal, row))
        self.assertTrue(self.bot.order_number_matches(self.first.split('A')[0], row))
        self.assertFalse(self.bot.order_number_matches(self.first.replace('-1', '-2'), row))

    def test_patrol_notification_keeps_both_numbers_and_does_not_substitute_erp(self):
        row = dict(billcode=self.internal, platform_no=f'{self.first}, {self.second}',
                   reason='请核对', task_state='', classification='blocked')
        report = dict(kind='today', end='2026-11-09T11:00:00+08:00',
                      shops=[dict(shopId='1', exceptions=[row, row, dict(row, platform_no='')],
                                  verified=0, scan=dict(complete=True))])
        message = self.bot.message_for(report)
        self.assertIn(f'{self.first}、{self.second}｜请核对（2 行）', message)
        self.assertIn('网店单号未知', message)
        self.assertNotIn(self.internal, message)

    def test_rematch_uses_resolved_web_numbers_without_a_second_lookup(self):
        row = dict(shopId='1', billcode=self.internal, platformNo=self.first)
        adapter = Mock()
        adapter.gateway.orders.return_value = ([row], 1)
        adapter.__enter__ = Mock(return_value=adapter)
        adapter.__exit__ = Mock(return_value=False)
        factory = lambda: adapter
        job = dict(id='fixture-job', kind='rematch_order', number=self.internal, shop='1')
        def legacy_run(job, root, adapter_factory, clock):
            self.bot.find_orders(job['number'], ['1'], None, None, adapter_factory)
            return dict(job, manualItems=[])
        legacy = types.SimpleNamespace(run_manual=legacy_run, message=lambda r: '结果\n号码：'+r['number'])
        with patch.dict(sys.modules, manual_match_jobs=legacy, batch_match=types.SimpleNamespace(order_gate=Mock())):
            report = self.bot.run_job(job, Path('.'), factory, datetime.now)
            message = self.bot.message_for(report)
        self.assertEqual(adapter.gateway.orders.call_count, 1)
        self.assertEqual(message, f'结果\n网店单号：{self.first}')

    def test_failed_order_notification_retains_submitted_web_number(self):
        message = self.bot.message_for(dict(jobId='fixture-job', kind='order', number=self.first, error='未找到'))
        self.assertIn(f'网店单号：{self.first}', message)

    def test_candidate_preserves_live_patrol_range(self):
        store = Mock()
        store.db.execute.return_value.fetchall.return_value = []
        worker = Mock()
        worker.scan.return_value = dict(complete=False, scanId='fixture')
        now = datetime.fromisoformat('2026-11-09T11:00:00+08:00')
        job = dict(id='fixture-job', kind='today', shop='1', number='')
        with patch.object(self.bot, 'TaskStore', return_value=store), patch.object(self.bot, 'PatrolWorker', return_value=worker):
            self.bot.run_job(job, clock=lambda: now)
        worker.scan.assert_called_once_with('1', datetime.fromisoformat('2026-11-08T00:00:00+08:00'), now)
