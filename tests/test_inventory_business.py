"""ReUI 业务回归：隔离数据、模拟网络；覆盖预览确认、身份和凭据边界。"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from inventory_refactor_check import seed
from douyin_inventory import DouyinInventory
from inventory_business import BusinessConsole,MODULES
from inventory_business_actions import ACTIONS

class BusinessTests(unittest.TestCase):
    def setUp(self):
        output=Path(__file__).resolve().parents[1]/'outputs/reui-business-tests'
        output.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=output))
        self.service=seed(self.root)
        self.controller=Mock(root=self.root)
        self.controller.status.return_value=dict(state='paused',ready=0,sources=[])
        self.runtime=SimpleNamespace(store=DouyinInventory(self.root),activity=lambda ident:'',submit=Mock())
        self.business=BusinessConsole(self.service,self.controller,self.runtime,offline=True)

    def tearDown(self):self.service.close_source()

    def act(self,module,name,values=None):return ACTIONS[(module,name)](self.business,values or {})

    def test_monitor_filters_apply_to_watched_products_and_exact_ids(self):
        # 顶部筛选必须改变当前关注列表，而非只影响另一个ERP目录分区。
        self.service.save_product(dict(sku='FILTER-A',goods_id='987654321',name='筛选专用 Alpha',threshold=5,enabled=True))
        self.service.save_product(dict(sku='FILTER-B',goods_id='987654322',name='筛选专用 Beta',threshold=5,enabled=True))
        from inventory_business_views import build_view
        def rows(query):return build_view(self.business,'monitor',query)['sections'][0]['tables'][0]['rows']
        self.assertEqual([r['sku'] for r in rows(dict(query='Alpha',mode='name'))],['FILTER-A'])
        self.assertEqual([r['sku'] for r in rows(dict(query='987654321',mode='goods_id'))],['FILTER-A'])
        self.assertEqual(rows(dict(query='98765432',mode='goods_id')),[])
        self.assertGreaterEqual(len(rows({})),2)

    def test_every_page_is_serializable_and_forms_have_allowlisted_handlers(self):
        for module in MODULES:
            view=self.business.snapshot(module,{})
            self.assertTrue(view['sections'])
            json.dumps(view,ensure_ascii=False)
            for section in view['sections']:
                forms=list(section['forms'])
                for table in section['tables']:
                    for row in table['rows']:forms.extend(row.get('_forms',[]))
                for form in forms:self.assertIn((form.get('module',module),form['action']),ACTIONS)

    def test_stored_secrets_are_never_returned_in_settings(self):
        with self.service.db() as db:
            self.service.put(db,'sql_connection',dict(server='localhost',port=1433,database='test',user='demo',password='SENTINEL_SECRET'))
            self.service.put(db,'daily_ai',dict(url='https://example.invalid',model='sample',key='SENTINEL_SECRET'))
        for module in ('settings','daily'):
            payload=json.dumps(self.business.snapshot(module,{}))
            self.assertNotIn('SENTINEL_SECRET',payload)

    def test_product_create_edit_duplicate_and_remove_stale_review(self):
        value=dict(sku='NEW',goods_id='123',name='测试商品',category='内存',threshold='10',enabled=True)
        self.act('monitor','save',value)
        with self.assertRaisesRegex(ValueError,'已关注'):self.act('monitor','save',value)
        result=self.act('monitor','remove-preview',dict(skus=['NEW']))
        token=result['forms'][0]['values']['token']
        self.act('monitor','save',dict(value,editing=True,threshold='20'))
        with self.assertRaisesRegex(ValueError,'已变化'):self.act('monitor','remove-confirm',dict(token=token))
        self.assertEqual(20,next(p for p in self.service.products() if p['sku']=='NEW')['threshold'])

    def test_import_cannot_confirm_ambiguous_or_changed_identity(self):
        file=self.root/'import.csv';file.write_text('goodsid,名称,预警值\n94015,测试,12\n',encoding='utf-8')
        result=self.act('monitor','import-preview',dict(path=str(file)))
        token=result['forms'][-1]['values']['token']
        self.act('monitor','import-confirm',dict(token=token))
        self.assertEqual(12,next(p for p in self.service.products() if p['goods_id']=='94015')['threshold'])
        with self.assertRaisesRegex(ValueError,'已变化'):self.act('monitor','import-confirm',dict(token=token))
        file.write_text('goodsid,名称,预警值\n999999,不存在,12\n',encoding='utf-8')
        result=self.act('monitor','import-preview',dict(path=str(file)))
        with self.assertRaisesRegex(ValueError,'异常行'):self.act('monitor','import-confirm',result['forms'][-1]['values'])

    def test_reviews_cannot_be_swapped_between_destructive_actions(self):
        token=self.business.review('report',dict(path=self.root/'report.md'))
        with self.assertRaisesRegex(ValueError,'预览已过期'):self.act('settings','cache-confirm',dict(token=token))
        with self.assertRaisesRegex(ValueError,'预览已过期'):self.act('monitor','remove-confirm',dict(token=token))

    def test_offline_mode_blocks_real_collection_and_delivery(self):
        for module,action in [('douyin','check-all'),('dingtalk','start'),('profit','check'),('daily','generate'),('daily','report-send')]:
            with self.assertRaisesRegex(ValueError,'隔离候选'):self.act(module,action)
        self.controller.start.assert_not_called()

    def test_shop_identity_and_rules_stay_scoped(self):
        stores=self.runtime.store.stores();first,second=stores[:2]
        before=self.runtime.store.low_stock_rules(second['id'])
        self.act('douyin','rules',dict(shop=first['id'],default=3,parts=[dict(goods_id='123',threshold=7,muted=False)]))
        self.assertEqual(before,self.runtime.store.low_stock_rules(second['id']))
        self.assertEqual(7,self.runtime.store.low_stock_rules(first['id'])['parts']['123'])
        with patch('douyin_maintenance.inspect_maintenance',return_value=dict(shop_id='32',sheets=['配置'],path='sample.xlsx')):
            # 本店名称使用已知店铺映射，错误店铺维护表不可保存。
            from douyin_maintenance import KNOWN_SHOPS
            with patch.dict(KNOWN_SHOPS,{first['name']:'76'}):
                with self.assertRaisesRegex(ValueError,'编号'):self.act('douyin','maintenance-preview',dict(shop=first['id'],path='sample.xlsx'))
        self.runtime.submit.assert_not_called()

    def test_job_deduplication_busy_guard_and_unknown_action(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def slow(b,v):calls.append(1);entered.set();release.wait(2);return dict(message='完成')
        with patch.dict(ACTIONS,{('settings','interval'):slow}):
            self.business.submit('settings','interval',{},'request-0001');entered.wait(1)
            self.business.submit('settings','interval',{},'request-0001')
            with self.assertRaisesRegex(ValueError,'尚未结束'):self.business.submit('settings','interval',{},'request-0002')
            release.set()
            deadline=time.monotonic()+3
            while self.business.busy() and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual('done',self.business.job('request-0001')['state'])
            self.assertEqual(1,len(calls))
        with self.assertRaisesRegex(ValueError,'不支持'):self.business.submit('settings','shell',{},'request-0003')

    def test_report_delivery_uses_reviewed_recipient_revision(self):
        self.business.offline=False
        from inventory_business_actions import report_form
        path=self.root/'report.md';path.write_text('模拟报告',encoding='utf-8')
        targets=[dict(id='one',name='样例',enabled=True,revision='original')]
        with patch('inventory_report_delivery.destinations',return_value=targets):result=report_form(self.business,path)
        with patch('inventory_report_delivery.send_reviewed',return_value=[]) as send:
            self.act('daily','report-send',dict(result['forms'][-1]['values'],recipients=['one'],body='已审阅的模拟报告'))
            self.assertEqual(targets,send.call_args.args[3])
            self.assertEqual(path,send.call_args.args[1])


    def test_report_save_preserves_send_action_and_edited_body(self):
        # 保存不能切断后续发送流程，且发送默认值必须来自刚保存的正文。
        path=self.root/'report.md';path.write_text('旧正文',encoding='utf-8')
        token=self.business.review('report',dict(path=path,targets=[]))
        result=self.act('daily','report-save',dict(token=token,body='人工编辑正文'))
        send=next(f for f in result['forms'] if f['action']=='report-send')
        self.assertEqual('人工编辑正文',send['values']['body'])
        self.assertEqual('人工编辑正文',path.read_text(encoding='utf-8'))

    def test_queued_settings_remain_busy_until_persisted(self):
        # 模拟同店长采集：回执到达前不能解锁退出或软件更新。
        queued=[];entered=threading.Event()
        self.runtime.submit.side_effect=lambda operation,shop:(queued.append(operation),entered.set())
        self.business.submit('douyin','interval',dict(shop='sample',minutes=15),'queued-setting-01')
        self.assertTrue(entered.wait(2));self.assertTrue(self.business.busy())
        self.assertEqual('save_setting',queued[0]['kind'])
        queued[0]['reply'].put(dict(ok=True,message='已落盘'))
        deadline=time.monotonic()+3
        while self.business.busy() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.business.busy());self.assertEqual('done',self.business.job('queued-setting-01')['state'])

if __name__=='__main__':unittest.main()
