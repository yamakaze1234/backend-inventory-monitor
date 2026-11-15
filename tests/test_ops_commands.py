"""运营机器人固定解析和群设置的隔离测试，不访问生产数据。"""
import tempfile
import unittest
from pathlib import Path

from inventory_monitor import InventoryService
from ops_commands import COMMANDS, GroupCommands, parse_command, render_help


class CommandTests(unittest.TestCase):
    def test_remove_accepts_only_one_goods_id(self):
        self.assertEqual(parse_command('删除监控 56281'),('monitor.remove',dict(goods_id='56281')))
        for text in ('删除监控','删除监控 全部','删除监控 56281 56282','删除监控 显卡','删除监控 -1'):
            with self.assertRaises(ValueError):parse_command(text)

    def test_fixed_commands_and_full_order_numbers(self):
        self.assertEqual(parse_command(' 查库存  56281 '), ('inventory.get', {'goods_id': '56281'}))
        self.assertEqual(parse_command('查库存 5060 Ti 白色')[1], {'keyword': '5060 Ti 白色'})
        for number, kind in [('00123A-1', 'web_order_no'), ('NO-001', 'erp_no'), ('CK-001', 'outbound_no')]:
            self.assertEqual(parse_command('查订单状态 ' + number)[1], {'number': number, 'number_type': kind})
        self.assertEqual(parse_command('查订单状态 物流 SF001')[1]['number_type'], 'tracking_no')
        with self.assertRaises(ValueError):
            parse_command('查订单状态 物流 NO-001')

    def test_natural_inventory_questions_stay_in_read_only_command(self):
        for text, keyword in (
            ('查询5070ti 雪鹰 库存和erp价格', '5070ti 雪鹰'),
            ('查询 5070 Ti 雪鹰的库存和 ERP 价格？', '5070 Ti 雪鹰'),
            ('帮我查一下 5070 Ti 雪鹰 ERP 成本价', '5070 Ti 雪鹰'),
            ('查一下 5070 Ti 雪鹰库存', '5070 Ti 雪鹰'),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text), ('inventory.get', {'keyword':keyword}))
        self.assertEqual(parse_command('查询５６２８１库存'), ('inventory.get', {'goods_id':'56281'}))
        self.assertEqual(parse_command('查询商品 5070 Ti 雪鹰'), ('inventory.get', {'keyword':'5070 Ti 雪鹰'}))
        self.assertEqual(parse_command('查价格变化 56281'), ('price.changes', {'goods_id':'56281'}))
        for text in ('查询订单进度', '查询库存', '查询 5070 Ti；修改阈值库存', '查询 5070 Ti 库存然后调整价格'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)

    def test_help_only_lists_available_commands(self):
        text = render_help(['help', 'inventory.get'])
        self.assertIn('查询商品 56281', text)
        self.assertNotIn('添加监控', text)
        with self.assertRaises(ValueError):
            render_help(['inventory.get'])

    def test_threshold_zero_and_invalid(self):
        self.assertEqual(parse_command('修改监控阈值 56281 为 0')[1]['threshold'], 0)
        for text in ('添加监控 56281', '添加监控 56281 阈值 -1', '添加监控 56281 阈值 100000001'):
            with self.assertRaises(ValueError):
                parse_command(text)

    def test_group_binding_and_permissions_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InventoryService(Path(directory))
            groups = GroupCommands(service)
            self.assertEqual(groups.available('o', 'r', 'unknown', COMMANDS, COMMANDS), [])
            self.assertEqual(groups.save('o', 'r', 'one'), list(COMMANDS))
            groups.save('o', 'r', 'two', [])
            groups.save('o', 'r', 'one', ['help', 'inventory.get'])
            self.assertEqual(groups.save('o', 'r', 'two'), [])
            reopened = GroupCommands(service)
            self.assertEqual(reopened.get('o', 'r', 'two'), [])
            self.assertEqual(reopened.available('o', 'r', 'one', ['help'], COMMANDS), ['help'])
            self.assertEqual(reopened.available('o', 'r', 'one', COMMANDS, ['inventory.get']), ['inventory.get'])
            self.assertIsNone(reopened.get('o', 'other', 'one'))


if __name__ == '__main__':
    unittest.main()
