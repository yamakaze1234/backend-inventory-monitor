"""隔离验证成员点选、模板身份绑定，以及确认前不会修改权限。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from openpyxl import load_workbook
from inventory_business import BusinessConsole
from inventory_monitor import InventoryService
from ops_commands import GroupCommands
from ops_member_discovery import MemberDirectory
from ops_settings import (choose_member, confirm_import, current, list_members, open_member,
                          preview_import, save, save_members, search_members, template)


class PermissionsTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.service = InventoryService(self.root)
        self.business = BusinessConsole(self.service)
        self.identity = ('org', 'ding-robot', 'cid-group')
        self.values = dict(zip(('org_id','robot_id','group_id'), self.identity))
        self.member, self.other = '1234567890123456789', '0987654321098765432'
        self.grant = dict(commands=['help'], shops=['76'], fields=[])
        self.groups = GroupCommands(self.service)
        self.groups.save(*self.identity, ['help','inventory.get'], members={self.member:self.grant}, name='样例群')
        self.second_group = ('org','ding-robot','cid-other')
        self.groups.save(*self.second_group, ['help'], members={'keep':self.grant}, name='另一群')
        (self.root/'order-bot.json').write_text(json.dumps(dict(version=1, shops=[dict(id='76', name='样例店')])), encoding='utf-8')
        self.directory = MemberDirectory()
        self.directory.call = Mock(side_effect=self.dws)

    def dws(self, args, profile=None):
        if args == ['profile','list']:
            return dict(profiles=[dict(profile='org:account', isOrgCurrent=True)])
        self.assertEqual(profile,'org:account')
        if args[1]=='+chat-members-list':
            return dict(complete=True, conversationId=args[3], users=[
                dict(openDingtalkId='D-a',name='样例甲',nick='小甲'),
                dict(openDingtalkId='D-b',name='样例乙',nick=''),
                dict(openDingtalkId='D-c',name='样例乙',nick='')])
        if args[1:3]==['user','search']:
            return dict(result=[dict(userId=self.member,openDingTalkId='D-a'),
                                dict(userId=self.other,openDingTalkId='D-b'),
                                dict(userId='unrelated',openDingTalkId='D-absent')])
        if args[1:3]==['user','get']:
            return dict(result=[dict(orgEmployeeModel=dict(orgUserId=self.member,orgUserName='样例甲'))])
        raise AssertionError(args)

    def book(self, rows):
        generated = template(self.business, self.values, client=self.directory)
        path = self.business.reviewed(generated['download'], 'download')
        book = load_workbook(path)
        data = book['成员权限']
        self.assertEqual(len(data.data_validations.dataValidation), 2)
        self.assertEqual(book['成员选项'].sheet_state, 'hidden')
        self.assertIsNone(data['A2'].value)
        headers = {cell.value:cell.column for cell in data[1]}
        for index, values in enumerate(rows, 2):
            for header, value in values.items():
                data.cell(index, headers[header], value)
        dest = self.root/'导入.xlsx'
        book.save(dest)
        book.close()
        return str(dest)

    def label_for(self, open_id):
        roster = self.directory.roster('org:account', self.identity[2])
        return next(k for k,v in roster.items() if v['open_id']==open_id)

    def test_template_merges_confirmed_names_and_preserves_other_members(self):
        label = self.label_for('D-b')
        path = self.book([{'群成员':label,'功能｜帮助':'是','功能｜查询商品':'是','店铺｜样例店':'是','附加｜ERP 成本和成本变化':'是'}])
        preview = preview_import(self.business,dict(self.values,path=path),client=self.directory)
        self.assertIn('新增 1 人',preview['message'])
        self.assertEqual(preview['tables'][0]['rows'][0]['commands'],'帮助、查询商品')
        self.assertEqual(current(self.service,self.identity)['members'],{self.member:self.grant})
        self.assertEqual(confirm_import(self.business,preview['forms'][0]['values'],client=self.directory),2)
        members = current(self.service,self.identity)['members']
        self.assertEqual(members[self.member],self.grant)
        self.assertEqual(members[self.other],dict(commands=['help','inventory.get'],shops=['76'],fields=['erp_cost']))
        self.assertEqual(current(self.service,self.second_group)['members'],{'keep':self.grant})
        save(self.service,dict(self.values,name='样例群',help=['help'],members=[]))
        self.assertEqual(current(self.service,self.identity)['members'],members)

    def test_wrong_group_bad_cells_and_unknown_identity_never_write(self):
        label = self.label_for('D-b')
        path = self.book([{'群成员':label,'功能｜帮助':'是'}, {'群成员':label,'功能｜帮助':'是'},
                          {'群成员':'=1+1'}, {'群成员':'同名但不在群'}, {'群成员':'样例甲（小甲）','功能｜帮助':'开'}])
        preview = preview_import(self.business,dict(self.values,path=path),client=self.directory)
        self.assertNotIn('forms',preview)
        self.assertEqual(len(preview['tables'][-1]['rows']),4)
        with self.assertRaisesRegex(ValueError,'另一个群'):
            preview_import(self.business,dict(self.values,group_id='cid-other',path=path),client=self.directory)
        self.assertEqual(current(self.service,self.identity)['members'],{self.member:self.grant})

    def test_pick_uses_open_identity_and_cannot_overwrite_stale_preview(self):
        path = self.book([{'群成员':self.label_for('D-b'),'功能｜帮助':'是'}])
        preview = preview_import(self.business,dict(self.values,path=path),client=self.directory)
        options = search_members(self.business,dict(self.values,query='样例乙'),client=self.directory)['nextForm']
        self.assertEqual(len(options['fields'][0]['options']),2)
        self.assertEqual(options['values']['choice'],'')
        with self.assertRaisesRegex(ValueError,'请选择'):
            choose_member(self.business,options['values'],client=self.directory)
        pick = next(o['value'] for o in options['fields'][0]['options'] if o['label']==self.label_for('D-b'))
        form = choose_member(self.business,dict(options['values'],choice=pick),client=self.directory)['nextForm']
        self.assertTrue(all(f['kind']=='multi' for f in form['fields']))
        self.assertNotIn('user_id',form['values'])
        self.assertEqual(save_members(self.business,dict(form['values'],commands=['inventory.get'],shops=['76']),
                                      client=self.directory),2)
        with self.assertRaisesRegex(ValueError,'权限已经变化'):
            confirm_import(self.business,preview['forms'][0]['values'],client=self.directory)
        existing = list_members(self.business,self.values,client=self.directory)['nextForm']
        item = next(o['value'] for o in existing['fields'][0]['options'] if o['label']==self.label_for('D-b'))
        editor = open_member(self.business,dict(existing['values'],choice=item))['nextForm']
        self.assertEqual(editor['values']['commands'],['inventory.get'])
        self.assertEqual(save_members(self.business,dict(editor['values'],remove=True)),1)
        self.assertEqual(current(self.service,self.identity)['members'],{self.member:self.grant})


if __name__ == '__main__':
    unittest.main()
