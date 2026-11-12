"""群名识别只填表，保存只信任核实的组织、机器人和群。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from inventory_business import BusinessConsole
from inventory_monitor import InventoryService
from ops_commands import GroupCommands
from ops_group_discovery import discover, resolve_choice
from ops_settings import current, save_binding, section


class GroupDiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = InventoryService(Path(directory.name))
        self.business = BusinessConsole(self.service)
        self.client = Mock()
        self.client.profiles.return_value = [dict(profile='org:account', label='组织 · 账号', current=True)]
        self.client.groups.return_value = [dict(group_id='cid-test', group_name='测试群')]
        self.client.bots.return_value = [dict(robot_code='ding-active', robot_name='运营机器人'),
                                         dict(robot_code='ding-other', robot_name='其他机器人')]

    def test_unique_group_prefills_verified_ids_and_preserves_members(self):
        self.business.ops_command_api = SimpleNamespace(robot_id='ding-active', org_id='org',
                                                        business=SimpleNamespace(implemented={'help','inventory.get'}))
        groups = GroupCommands(self.service)
        grants = {'member':dict(commands=['help'],shops=[],fields=[])}
        groups.save('org','ding-active','cid-test',['help'],members=grants,name='测试群')
        page = section(self.business)['forms'][0]
        self.assertEqual([f['key'] for f in page['fields']], ['name'])
        self.assertEqual(page['submitLabel'], '识别群信息')
        result = discover(self.business, {'name':' 测试群 '}, client=self.client)
        form = result['nextForm']
        self.assertEqual(form['values']['robot_id'],'ding-active')
        self.assertEqual(form['values']['org_id'],'org')
        self.assertEqual(form['values']['group_id'],'cid-test')
        self.assertEqual(form['values']['help'], ['help'])
        self.assertEqual(form['values']['inventory'], [])
        self.assertTrue(all(f['kind']=='multi' for f in form['fields']))
        self.assertIn('测试群',form['description'])
        self.client.bots.assert_called_once_with('org:account','cid-test')
        with self.assertRaisesRegex(ValueError,'与识别结果不一致'):
            save_binding(self.business, dict(form['values'], group_id='cid-other'))
        self.assertEqual(save_binding(self.business, form['values']), ['help'])
        self.assertEqual(current(self.service, ('org','ding-active','cid-test'))['members'], grants)

    def test_duplicate_group_then_duplicate_robot_requires_choices(self):
        self.client.groups.return_value.append(dict(group_id='cid-test2', group_name='测试群'))
        result = discover(self.business, {'name':'测试群'}, client=self.client)['nextForm']
        self.assertEqual(result['action'], 'ops-group-group-choice')
        self.assertEqual(result['values']['choice'], '')
        self.client.bots.assert_not_called()
        with self.assertRaisesRegex(ValueError,'请选择'):
            resolve_choice(self.business, result['values'], 'group-choice', client=self.client)
        result = resolve_choice(self.business, dict(result['values'], choice='1'), 'group-choice', client=self.client)['nextForm']
        self.assertEqual(result['action'], 'ops-group-robot-choice')
        self.assertEqual(result['values']['choice'], '')
        self.client.bots.assert_called_once_with('org:account','cid-test2')
        self.client.verify.side_effect = lambda r: r
        form = resolve_choice(self.business, dict(result['values'], choice='0'), 'robot-choice', client=self.client)['nextForm']
        self.assertEqual(form['values']['group_id'], 'cid-test2')
        self.assertIsNone(GroupCommands(self.service).get('org','ding-active','cid-test2'))
        self.assertTrue(save_binding(self.business, form['values']))

    def test_new_binding_cannot_skip_discovery(self):
        GroupCommands(self.service)
        with self.assertRaisesRegex(ValueError,'群未绑定'):
            save_binding(self.business, dict(org_id='org', robot_id='ding-active', group_id='cid-forged',
                                            name='伪造', help=['help']))

    def test_configured_robot_absent_does_not_fall_back_to_another(self):
        with patch('ops_group_discovery.preferred', return_value=('org','ding-missing','org:account')):
            with self.assertRaisesRegex(ValueError,'未在此群找到当前运营机器人'):
                discover(self.business, {'name':'测试群'}, client=self.client)


if __name__ == '__main__':
    unittest.main()
