"""只用模拟DWS，验证名称解析不会跨组织或把重名群误当唯一目标。"""
import unittest
from unittest.mock import Mock,patch
from notification_resolver import resolve,verify_selection
from notification_business import search,candidate


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock()
        self.client.profiles.return_value=[dict(profile='current',current=True,label='本公司')]
        self.client.groups.return_value=[dict(group_id='cidone',group_name='部门群')]
        self.client.bots.return_value=[dict(robot_code='dingone',robot_name='部门助手')]

    def test_current_profile_exact_names_and_identifiers(self):
        result=resolve(self.client,'部门群','部门助手')
        self.assertTrue(result['exact'])
        self.assertEqual(result['targets'][0]['profile'],'current')
        self.client.groups.assert_called_once_with('current','部门群')
        self.client.bots.assert_called_once_with('current','cidone')

    def test_no_current_profile_requires_explicit_choice_even_if_only_one(self):
        self.client.profiles.return_value[0]['current']=False
        result=resolve(self.client,'部门群','部门助手')
        self.assertEqual(len(result['profiles']),1);self.client.groups.assert_not_called()
        with self.assertRaises(ValueError):resolve(self.client,'部门群','部门助手','other')

    def test_same_name_groups_and_partial_names_are_not_auto_selected(self):
        self.client.groups.return_value.append(dict(group_id='cidtwo',group_name='部门群'))
        result=resolve(self.client,'部门群','部门助手')
        self.assertFalse(result['exact']);self.assertEqual(len(result['targets']),2)
        self.client.groups.return_value=self.client.groups.return_value[:1]
        self.assertFalse(resolve(self.client,'部门','助手')['exact'])

    def test_stale_choice_cannot_be_saved(self):
        target=resolve(self.client,'部门群','部门助手')['targets'][0]
        self.client.bots.return_value=[dict(robot_code='dingchanged',robot_name='部门助手')]
        with self.assertRaisesRegex(ValueError,'已变化'):verify_selection(self.client,target)

    def test_search_ignores_injected_internal_ids_and_autofills_name(self):
        b=Mock();store=b.robots.return_value
        values=dict(mode='enterprise',group_name='部门群',robot_name='部门助手',profile='untrusted',group_id='cidwrong',robot_code='dingwrong',channels=['profit'],enabled=False)
        with patch('notification_business.EnterpriseClient',return_value=self.client):search(b,values)
        args=store.save.call_args
        self.assertEqual(args.kwargs['enterprise']['group_id'],'cidone')
        self.assertEqual(args.args[0],'部门助手 · 部门群')
        self.assertFalse(args.kwargs['enabled'])

    def test_ambiguous_search_saves_only_after_server_reviewed_selection(self):
        self.client.groups.return_value.append(dict(group_id='cidtwo',group_name='部门群'))
        b=Mock();saved={}
        b.review.side_effect=lambda kind,value:saved.update(kind=kind,value=value) or 'review-key'
        b.reviewed.side_effect=lambda token,kind:saved['value']
        with patch('notification_business.EnterpriseClient',return_value=self.client):
            result=search(b,dict(group_name='部门群',robot_name='部门助手',channels=['profit']))
            b.robots.return_value.save.assert_not_called()
            self.assertEqual(result['forms'][0]['action'],'robot-candidate')
            candidate(b,dict(token='review-key',choice='1'))
        self.assertEqual(b.robots.return_value.save.call_args.kwargs['enterprise']['group_id'],'cidtwo')


    def test_duplicate_group_name_still_requires_choice_if_only_one_has_bot(self):
        self.client.groups.return_value.append(dict(group_id='cidtwo',group_name='部门群'))
        self.client.bots.side_effect=lambda profile,group: [dict(robot_code='dingone',robot_name='部门助手')] if group=='cidone' else []
        self.assertFalse(resolve(self.client,'部门群','部门助手')['exact'])

    def test_edit_does_not_silently_rebind_replaced_same_name_robot(self):
        old=dict(profile='current',group_id='cidold',group_name='部门群',robot_code='dingold',robot_name='部门助手')
        b=Mock();b.robots.return_value.list_robots.return_value=[dict(id='saved',mode='enterprise',enterprise=old)]
        with patch('notification_business.EnterpriseClient',return_value=self.client):
            result=search(b,dict(id='saved',group_name='部门群',robot_name='部门助手',channels=['profit']))
        b.robots.return_value.save.assert_not_called()
        self.assertEqual(result['forms'][0]['action'],'robot-candidate')
