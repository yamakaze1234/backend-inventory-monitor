"""订单配置表单边界：合成目标，不部署后端或发送真实消息。"""
import unittest
import tempfile
from unittest.mock import Mock
from order_business import patrol_window_form,save_patrol_window, search_robot,select_robot,shipping_action,view


class OrderBusinessTests(unittest.TestCase):
    def setUp(self):
        self.b=Mock()
        self.b.service.root=tempfile.mkdtemp(prefix="order-business-test-")
        self.result=dict(profile='test-profile',robots=[dict(appId='app-A',name='接单助手')],groups=[dict(groupId='group-A',name='部门群')])
        self.b.order_bot.action.side_effect=lambda action,values:self.result if action=='discover-robots' else dict(message='已保存')
        self.b.order_bot.configuration.return_value=dict(configured=True,deployed=True,patrolWindowReady=True,
                                                          target=dict(intervalMinutes=240))
        self.values=dict(groupQuery='部门群',robotQuery='接单助手')

    def test_exact_target_uses_discovered_ids(self):
        search_robot(self.b,{**self.values,'appId':'injected','groupId':'injected'})
        action,values=self.b.order_bot.action.call_args.args
        self.assertEqual(action,'save-robot')
        self.assertEqual(values['appId'],'app-A')
        self.assertEqual(values['groupId'],'group-A')
        self.assertEqual(values['intervalMinutes'],240)

    def test_ambiguous_target_requires_review_and_rejects_negative_index(self):
        self.result['groups'].append(dict(groupId='group-B',name='部门群'))
        search_robot(self.b,self.values)
        self.assertEqual(self.b.order_bot.action.call_count,1)
        plan=self.b.review.call_args.args[1]
        self.b.reviewed.return_value=plan
        with self.assertRaises(ValueError):select_robot(self.b,dict(token='review',robot='0',group='-1'))
        select_robot(self.b,dict(token='review',robot='0',group='1'))
        self.assertEqual(self.b.order_bot.action.call_args.args[1]['groupId'],'group-B')

    def test_schedule_form_keeps_existing_interval_and_saves_without_rebinding(self):
        robot=view(self.b,{})['sections'][1]
        self.assertEqual(robot['planLink'],'orders')
        self.assertEqual([form['action'] for form in robot['forms']],['robot-search'])
        form=patrol_window_form(self.b.order_bot.configuration())
        self.assertEqual(form['values']['intervalMinutes'],240)
        self.assertEqual(form['module'],'order-settings')
        self.b.order_bot.action.return_value=dict(start='18:30',end='00:00',nextDay=True,intervalMinutes=90)
        self.b.order_bot.action.side_effect=None
        payload=dict(start='18:30',end='00:00',intervalMinutes='90')
        for minutes in (1,1441,'bad','90.5',True):
            with self.assertRaises(ValueError):
                save_patrol_window(self.b,{**payload,'intervalMinutes':minutes})
        self.b.order_bot.action.assert_not_called()
        result=save_patrol_window(self.b,payload)
        self.b.order_bot.action.assert_called_once_with('save-patrol-window',dict(payload,intervalMinutes=90))
        self.assertIn('每 90 分钟',result['message'])

    def test_incomplete_results_never_save(self):
        self.result['moreRobots']=True
        with self.assertRaises(ValueError):
            search_robot(self.b,self.values)
        self.assertEqual(self.b.order_bot.action.call_count,1)

    def test_manual_shipping_requires_running_worker_without_enabling_schedule(self):
        state=dict(deployed=True,shippingReady=True,shipping=dict(bound=True,enabled=False),serviceEnabled=True)
        self.b.order_bot.configuration.return_value=state
        with self.assertRaisesRegex(ValueError,'执行器未运行'):
            shipping_action(self.b,'shipping')
        self.b.order_bot.action.assert_not_called()
        state['online']=True
        self.b.order_bot.action.side_effect=None
        self.b.order_bot.action.return_value=dict(jobId='shipping-1',status='queued')
        self.assertIn('已入队',shipping_action(self.b,'shipping')['message'])
        self.b.order_bot.action.assert_called_once_with('shipping')
