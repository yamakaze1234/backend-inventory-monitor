"""订单配置表单边界：合成目标，不部署后端或发送真实消息。"""
import unittest
from unittest.mock import Mock
from order_business import search_robot,select_robot


class OrderBusinessTests(unittest.TestCase):
    def setUp(self):
        self.b=Mock()
        self.result=dict(profile='test-profile',robots=[dict(appId='app-A',name='接单助手')],groups=[dict(groupId='group-A',name='部门群')])
        self.b.order_bot.action.side_effect=lambda action,values:self.result if action=='discover-robots' else dict(message='已保存')
        self.values=dict(groupQuery='部门群',robotQuery='接单助手',intervalMinutes=120)

    def test_exact_target_uses_discovered_ids(self):
        search_robot(self.b,{**self.values,'appId':'injected','groupId':'injected'})
        action,values=self.b.order_bot.action.call_args.args
        self.assertEqual(action,'save-robot')
        self.assertEqual(values['appId'],'app-A')
        self.assertEqual(values['groupId'],'group-A')

    def test_ambiguous_target_requires_review_and_rejects_negative_index(self):
        self.result['groups'].append(dict(groupId='group-B',name='部门群'))
        search_robot(self.b,self.values)
        self.assertEqual(self.b.order_bot.action.call_count,1)
        plan=self.b.review.call_args.args[1]
        self.b.reviewed.return_value=plan
        with self.assertRaises(ValueError):select_robot(self.b,dict(token='review',robot='0',group='-1'))
        select_robot(self.b,dict(token='review',robot='0',group='1'))
        self.assertEqual(self.b.order_bot.action.call_args.args[1]['groupId'],'group-B')

    def test_invalid_interval_and_incomplete_results_never_save(self):
        for minutes in (1,1441,'bad'):
            with self.assertRaises(ValueError):search_robot(self.b,{**self.values,'intervalMinutes':minutes})
        self.b.order_bot.action.assert_not_called()
        self.result['moreRobots']=True
        with self.assertRaises(ValueError):search_robot(self.b,self.values)
        self.assertEqual(self.b.order_bot.action.call_count,1)
