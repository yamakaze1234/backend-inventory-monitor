"""权限模板与导入的隔离验证：权限只影响所选群，确认前不落库。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook
from inventory_business import BusinessConsole
from inventory_monitor import InventoryService
from ops_commands import GroupCommands
from ops_settings import confirm_import, current, preview_import, save, save_members, stamp, template


class PermissionsImportTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.service = InventoryService(self.root)
        self.business = BusinessConsole(self.service, offline=True)
        self.identity = ('org', 'robot', 'group')
        self.values = dict(zip(('org_id','robot_id','group_id'), self.identity))
        self.member = '1234567890123456789'
        self.other = '0987654321098765432'
        self.grant = dict(commands=['help'], shops=['76'], fields=[])
        self.groups = GroupCommands(self.service)
        self.groups.save(*self.identity, ['help','inventory.get'], members={self.member:self.grant}, name='样例群')
        self.groups.save('org','robot','other', ['help'], members={'keep':self.grant}, name='另一群')
        (self.root/'order-bot.json').write_text(json.dumps(dict(version=1, shops=[dict(id='76', name='样例店')])), encoding='utf-8')

    def book(self, rows):
        generated = template(self.business, self.values)
        path = self.business.reviewed(generated['download'], 'download')
        book = load_workbook(path)
        self.assertEqual(book['成员权限']['A2'].number_format, '@')
        self.assertIn('填写说明', book.sheetnames)
        # 模板本身不含任何已有成员记录；填入的是隔离用例的稳定 ID。
        self.assertIsNone(book['成员权限']['A2'].value)
        for row, fields in enumerate(rows, 2):
            for col, value in enumerate(fields, 1):
                book['成员权限'].cell(row, col, value=value)
        dest = self.root/'导入.xlsx'
        book.save(dest)
        book.close()
        return str(dest)

    def test_import_merges_only_listed_members_after_confirmation(self):
        path = self.book([[self.other,'help,inventory.get','76','erp_cost']])
        preview = preview_import(self.business, dict(self.values, path=path))
        self.assertIn('新增 1 人', preview['message'])
        self.assertEqual(current(self.service,self.identity)['members'], {self.member:self.grant})
        result = confirm_import(self.business, preview['forms'][0]['values'])
        self.assertEqual(result, 2)
        members = current(self.service,self.identity)['members']
        self.assertEqual(members[self.member], self.grant)
        self.assertEqual(members[self.other]['commands'], ['help','inventory.get'])
        self.assertEqual(current(self.service,('org','robot','other'))['members'], {'keep':self.grant})
        # 群功能编辑保留成员，旧界面传来的成员字段也不会覆盖新授权。
        save(self.service, dict(self.values, name='样例群', help=['help'], members=[]))
        self.assertEqual(current(self.service,self.identity)['members'], members)

    def test_form_rejects_duplicate_or_formula_or_other_shop_without_writes(self):
        path = self.book([[self.other,'help','76',''], [self.other,'help','76',''],
                          ['=1+1','help','76',''], ['no-shop','help','99',''], [1234567890,'help','76','']])
        preview = preview_import(self.business, dict(self.values, path=path))
        self.assertNotIn('forms', preview)
        self.assertEqual(len(preview['tables'][-1]['rows']), 4)
        self.assertEqual(current(self.service,self.identity)['members'], {self.member:self.grant})

    def test_stale_preview_never_overwrites_manually_edited_permissions(self):
        path = self.book([[self.other,'help','76','']])
        preview = preview_import(self.business, dict(self.values, path=path))
        state = current(self.service,self.identity)
        save_members(self.business, dict(self.values, expected=stamp(state),
                    members=[dict(user_id=self.member, commands='inventory.get', shops='76', fields='')]))
        with self.assertRaisesRegex(ValueError, '权限已经变化'):
            confirm_import(self.business, preview['forms'][0]['values'])
        self.assertEqual(list(current(self.service,self.identity)['members']), [self.member])


if __name__ == '__main__':
    unittest.main()
