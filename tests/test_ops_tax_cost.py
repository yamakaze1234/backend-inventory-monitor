"""验证成本表按显式列连接、价格口径独立、权限投影及冲突处理。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook
from inventory_business import BusinessConsole
from inventory_monitor import InventoryService
from ops_command_api import Access, CommandAPI
from ops_inventory import InventoryCommands
from ops_replies import render
from ops_runtime import CommandBusiness
from ops_tax_cost import KEY, quote_costs
from ops_tax_cost_settings import inspect, preview, save


class TaxCostTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root/'主机成本表.xlsx'
        self.service = InventoryService(self.root)
        self.service.accept_snapshot('s', [dict(goods_id='86739',sku='SKU-86739',name='5070 Ti 雪鹰',stock=8,able=8,purchase=0)], scope='fixture')
        self.business = BusinessConsole(self.service,offline=True)
        book=Workbook()
        sheet=book.active
        sheet.title='库存数据'
        sheet.append(['goods_id','旧数据','goods_id','ERP名称','含税（核算）成本','ERP成本'])
        sheet.append([777,'旧',86739,'雪鹰',7800,11600])
        sheet.append([778,'旧',86739,'雪鹰',7800,11600])
        sheet.append([None,None,333,'冲突商品',100,200])
        sheet.append([None,None,333,'冲突商品',101,200])
        sheet.append([None,None,444,'零成本',0,200])
        sheet.append([None,None,555,'缓存缺失','=5+5',200])
        book.save(self.path)
        book.close()

    def offer(self):
        choice = inspect(self.business,dict(path=str(self.path)))['nextForm']
        self.assertEqual(choice['values']['choice'],'')
        opts=choice['fields'][0]['options']
        self.assertEqual(len(opts),2)
        entry=next(o for o in opts if 'C goods_id' in o['label'])
        return preview(self.business,dict(choice['values'],choice=entry['value']))

    def test_verify_and_save_the_selected_cost_columns(self):
        offer=self.offer()
        self.assertEqual(offer['tables'][0]['rows'][0],dict(goods_id='86739',name='5070 Ti 雪鹰',amount='¥7,800.00'))
        with self.service.db() as db:
            self.assertIsNone(self.service.get(db,KEY))
        save(self.business,offer['forms'][0]['values'])
        data=quote_costs(self.service,{'86739','333','444','555','999'})
        self.assertEqual(data['86739']['tax_cost_cents'],780000)
        self.assertEqual(data['333']['tax_cost_status'],'conflict')
        self.assertIsNone(data['333']['tax_cost_cents'])
        self.assertEqual(data['444']['tax_cost_cents'],0)
        self.assertEqual(data['555']['tax_cost_status'],'unknown')
        self.assertEqual(data['999']['tax_cost_status'],'no_match')

    def test_group_reply_separates_erp_cost_and_respects_revocation(self):
        save(self.business,self.offer()['forms'][0]['values'])
        inv=InventoryCommands(self.service,cost=lambda ids:dict(costs={'86739':1160000},collectedAt=1794386360))
        api=CommandAPI(self.service,CommandBusiness(inv),'robot','x'*40)
        def grant(fields):
            api.groups.save('org','robot','group',['inventory.get'],members={'user':dict(commands=['inventory.get'],shops=[],fields=fields)})
        def request():
            return api.submit(dict(request_id='dingtalk:cost-quote',command='inventory.get',
                                   actor=dict(org_id='org',group_id='group',user_id='user'),args={'goods_id':'86739'}))[1]
        grant(['erp_cost','tax_cost'])
        msg=render(request())
        self.assertIn('ERP 库存成本 ¥11,600.00',msg)
        self.assertIn('含税（核算）成本 ¥7,800.00',msg)
        grant(['erp_cost'])
        replay=request()
        self.assertTrue(replay['replayed'])
        self.assertNotIn('含税（核算）成本',render(replay))
        self.assertNotIn('tax_cost_source',replay['data']['items'][0])

    def test_changed_workbook_requires_a_new_preview(self):
        offer=self.offer()
        with self.path.open('ab') as f:
            f.write(b'new')
        with self.assertRaisesRegex(ValueError,'预览后已更新'):
            save(self.business,offer['forms'][0]['values'])


if __name__ == '__main__':
    unittest.main()
