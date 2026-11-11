"""用隔离订单验证网店单号匹配和通知，避免访问真实 ERP 和群。"""
import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import closing
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

    def queue_root(self):
        base = Path(__file__).resolve().parents[1] / '.codex_tmp/order-repeat-tests'
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=base))

    def test_manual_patrol_can_repeat_immediately_after_previous_run_finishes(self):
        root = self.queue_root()
        with patch.object(self.bot.time, 'time', return_value=1000):
            first = self.bot.enqueue('today', root=root)
            for state in ('completed', 'needs_review'):
                with closing(self.bot.queue_db(root)) as db:
                    db.execute('UPDATE jobs SET state=? WHERE id=?', (state, first['jobId']))
                second = self.bot.enqueue('today', root=root)
                self.assertEqual(second['status'], 'queued')
                self.assertFalse(second['duplicate'])
                self.assertNotEqual(second['jobId'], first['jobId'])
                first = second

    def test_manual_patrol_reuses_active_or_unknown_task(self):
        root = self.queue_root()
        original = self.bot.enqueue('today', root=root)
        for state in ('queued', 'running', 'unknown'):
            with closing(self.bot.queue_db(root)) as db:
                # 模拟旧任务，确认超过原来的 120 秒仍保护在途和未知结果。
                db.execute('UPDATE jobs SET state=?,created=0 WHERE id=?', (state, original['jobId']))
            result = self.bot.enqueue('today', root=root)
            self.assertEqual((result['jobId'], result['status']), (original['jobId'], state))
            self.assertTrue(result['duplicate'])
            self.assertIn('巡检', result['message'])

    def test_manual_patrol_can_take_over_queued_schedule_while_paused(self):
        root = self.queue_root()
        with patch.object(self.bot.time, 'time', return_value=1000):
            first = self.bot.enqueue('today', origin='schedule', root=root)
            with closing(self.bot.queue_db(root)) as db:
                db.execute("INSERT INTO settings VALUES('schedulePaused','1')")
            manual = self.bot.enqueue('today', root=root)
            self.assertEqual(manual['jobId'], first['jobId'])
            with closing(self.bot.queue_db(root)) as db:
                self.assertEqual(db.execute('SELECT origin FROM jobs').fetchone()[0], 'manual')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
                db.execute("UPDATE jobs SET state='completed'")
            self.assertEqual(self.bot.enqueue('today', origin='schedule', root=root)['jobId'], first['jobId'])

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
        self.assertIn(f'{self.first}、{self.second}\n请核对（2 行）', message)
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

    def test_daily_patrol_uses_unmatched_adapter_without_changing_manual_defaults(self):
        """每日巡检关掉已关联补查；不修改共享类，人工重匹配仍使用原入口。"""
        class Adapter:
            audit_linked = True
            unmatched_only = False
        store = Mock();store.db.execute.return_value.fetchall.return_value = []
        worker = Mock();worker.scan.return_value = dict(complete=False, scanId='fixture')
        with patch.object(self.bot, 'TaskStore', return_value=store), patch.object(self.bot, 'PatrolWorker', return_value=worker) as constructor:
            self.bot.run_job(dict(id='fixture',kind='today',shop='1',number=''), adapter_factory=Adapter)
        adapter=constructor.call_args.args[1]()
        self.assertFalse(adapter.audit_linked)
        self.assertTrue(adapter.unmatched_only)
        self.assertTrue(Adapter.audit_linked)
        self.assertFalse(Adapter.unmatched_only)

    def test_order_counts_deduplicate_lines_and_do_not_count_partial_success(self):
        """同单多行只算一单；部分成功归待处理，合单保留各网店单号。"""
        row=dict(platform_no=self.first,billcode='internal',classification='ready',readback_state='verified')
        self.assertEqual(self.bot.order_summary([row,row]),dict(success=1,pending=0))
        self.assertEqual(self.bot.order_summary([row,dict(row,readback_state=None)]),dict(success=0,pending=1))
        self.assertEqual(self.bot.order_summary([dict(row,platform_no=self.first+','+self.second)]),dict(success=2,pending=0))

    def test_patrol_compact_shop_totals_and_real_elapsed(self):
        """验证店铺、订单单位和耗时；扫描失败不能显示完成。"""
        shop=dict(shopId='1',verified=2,exceptions=[],scan=dict(complete=True),orderSummary=dict(success=2,pending=0))
        report=dict(kind='today',shops=[shop],elapsedSeconds=26.82)
        text=self.bot.message_for(report)
        self.assertIn('自动匹配完成｜耗时 27 秒',text)
        self.assertIn('样例店：成功 2 单｜待处理 0 单',text)
        shop['scan']['complete']=False
        self.assertIn('自动匹配未完成',self.bot.message_for(report))
