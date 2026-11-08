"""维护表价格清单的身份、数量、完整性与重新读取验证。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook
from price_maintenance import inspect_sources, read_sources, saved_sources
from profit_manifest import read_manifest

PID = '1234567890123456789'
SID = '2234567890123456789'


class PriceMaintenanceTests(unittest.TestCase):
    def setUp(self):
        # 测试只生成隔离样例，不接触正式维护表或 ERP。
        base = Path(__file__).resolve().parents[1] / '.codex_tmp/price-maintenance-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))

    def book(self, rows=None, shop=76, filename='维护表.xlsx'):
        book = Workbook(); sheet = book.active; sheet.title = '综合表'
        sheet.append(['SPU', 'SKU', '简称', 'CPU_id', 'CPU', 'CPU_数量',
                      '机箱_id', '机箱', '机箱_数量', '配件1_id', '配件1', '配件1_数量'])
        for row in rows if rows is not None else [self.row()]:
            sheet.append(row)
        settings = book.create_sheet('禁止编辑'); settings['G2'] = shop
        settings['I6'] = ',{"goods_id":15,"n":2}'; settings['G26'] = 99
        path = self.root / filename; book.save(path); book.close()
        return [dict(path=str(path), sheet='综合表', shop_id=str(shop))]

    def row(self):
        return [PID, SID, '配置一', 12, 'CPU', 1, 99, '机箱', 1, 12, '重复配件', 2]

    def test_manifest_contract_exact_identity_quantities_and_extras(self):
        bindings = self.book(); manifest = read_sources(bindings)
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        read_manifest(path)
        config = manifest['configs'][0]
        self.assertEqual((config['shopId'], config['configId'], config['product']), ('76', PID + ':' + SID, PID))
        parts = {part['goodsId']: part for part in config['parts']}
        self.assertEqual({gid: part['qty'] for gid, part in parts.items()}, {'12': 3, '99': 1, '15': 2, '1651': 1})
        self.assertEqual(parts['12']['slot'], 'CPU／配件1')
        self.assertEqual(config['maintenance']['sku'], SID)
        self.assertEqual(inspect_sources(bindings)['goodsCount'], 4)

    def test_revision_unchanged_for_same_business_and_increments_after_saved_change(self):
        bindings = self.book(); before = read_sources(bindings)
        same = read_sources(bindings, before)
        self.assertEqual(same['revision'], before['revision'])
        self.assertEqual(inspect_sources(bindings), inspect_sources(bindings))
        row = self.row(); row[5] = 4; self.book([row])
        changed = read_sources(bindings, before)
        self.assertEqual(changed['revision'], before['revision'] + 1)
        self.assertNotEqual(changed['businessHash'], before['businessHash'])

    def test_confirmation_checks_the_same_parsed_content(self):
        bindings = self.book(); preview = inspect_sources(bindings)
        read_sources(bindings, expected=preview)
        row = self.row(); row[5] = 4; self.book([row])
        with self.assertRaisesRegex(ValueError, '预览后已变化'):
            read_sources(bindings, expected=preview)

    def test_duplicate_or_bad_id_rows_are_previewed_and_block_binding(self):
        for rows in ([self.row(), self.row()], [[PID, SID, '配置一', '', 'CPU', 1, 99, '机箱', 1, '', '', 0]],
                     [[PID, '', *self.row()[2:]]], [[int(PID), SID, *self.row()[2:]]]):
            with self.subTest(rows=rows):
                bindings = self.book(rows)
                preview = inspect_sources(bindings)
                self.assertFalse(preview['complete']); self.assertTrue(preview['issues'])
                with self.assertRaisesRegex(ValueError, '未更新清单'):
                    read_sources(bindings)

    def test_blank_spu_not_monitored_and_empty_scope_is_rejected(self):
        row = self.row(); row[0] = ''
        bindings = self.book([self.row(), row])
        self.assertEqual(inspect_sources(bindings)['count'], 1)
        bindings = self.book([row])
        with self.assertRaisesRegex(ValueError, '没有可监控'):
            read_sources(bindings)

    def test_group_heading_is_not_a_broken_configuration(self):
        bindings = self.book([['分组标题' + PID], ['分组标题' + PID, '活动直降'], ['分组标题' + PID, '下架'], self.row()])
        preview = inspect_sources(bindings)
        self.assertTrue(preview['complete'])
        self.assertEqual(preview['count'], 1)
        self.assertEqual(preview['files'][0]['excludedHeadings'], 3)

    def test_cross_file_duplicate_identity_is_not_double_counted(self):
        bindings = self.book() + self.book(filename='另一个维护表.xlsx')
        self.assertIn('相同店铺', inspect_sources(bindings)['issues'][0]['reason'])
        with self.assertRaisesRegex(ValueError, '未更新清单'):
            read_sources(bindings)

    def test_wrong_shop_and_duplicate_binding_rejected(self):
        bindings = self.book()
        with self.assertRaisesRegex(ValueError, '重复绑定'):
            read_sources(bindings + bindings)
        bindings[0]['shop_id'] = '32'
        with self.assertRaisesRegex(ValueError, '店铺编号'):
            read_sources(bindings)

    def test_saved_bindings_only_return_public_source_fields(self):
        store = Mock()
        store.stores.return_value = [dict(maintenance_path='sample.xlsx', maintenance_sheet='综合表',
                                         maintenance_shop_id='76', name='店铺', extra='不返回'), {}]
        self.assertEqual(saved_sources(store), [dict(path='sample.xlsx', sheet='综合表', shop_id='76')])

    def test_monitor_reloads_saved_ids_and_blocks_stale_scope_after_damage(self):
        from erp_price_monitor import ProfitMonitor
        source = Mock()
        source.read_profit_costs.side_effect = lambda ids: dict(sourceScope='test-only', costs={gid: 10000 for gid in ids})
        bindings = self.book(); monitor = ProfitMonitor(self.root / 'monitor', source_provider=lambda: source)
        monitor.bind_maintenance(bindings)
        monitor.check(allow_notifications=False)
        self.assertEqual(source.read_profit_costs.call_args.args[0], {'12', '99', '15', '1651'})
        row = self.row(); row[3] = 22; row[9] = 22
        self.book([row]); monitor.check(allow_notifications=False)
        self.assertEqual(source.read_profit_costs.call_args.args[0], {'22', '99', '15', '1651'})
        previous_calls = source.read_profit_costs.call_count
        row[3] = ''; self.book([row])
        with self.assertRaisesRegex(ValueError, '未更新清单'):
            monitor.check(allow_notifications=False)
        self.assertEqual(source.read_profit_costs.call_count, previous_calls)
        self.assertIn('未更新清单', monitor.status()['sourceError'])

    def test_change_during_validation_is_not_accepted(self):
        bindings = self.book()
        with patch('price_maintenance.digest', return_value='changed'):
            with self.assertRaisesRegex(ValueError, '读取期间'):
                read_sources(bindings)


if __name__ == '__main__':
    unittest.main()
