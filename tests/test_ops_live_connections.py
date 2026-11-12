"""群订单最终回执和现有在线表的隔离验证，不触发真实写入或通知。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from inventory_monitor import InventoryService
from ops_command_api import CommandAPI
from ops_commands import COMMANDS
from ops_live_tables import FIELDS, LiveReportTables
from ops_order_delivery import OrderDelivery
from ops_runtime import CommandBusiness


class LiveConnectionTests(unittest.TestCase):
    def test_orders_use_one_durable_final_reply_and_do_not_cross_group(self):
        with TemporaryDirectory() as directory:
            service=InventoryService(Path(directory))
            order=Mock()
            order.snapshot.return_value=dict(workerOnline=True,serviceEnabled=True,shops=[dict(id='76')])
            order.action.return_value=dict(jobId='job-1',status='queued')
            order.ops_task.return_value=dict(status='completed',task_id='job-1',shop_id='76',created_at=0,
                                             final_reply_owned=True,success=1,skipped=0,review=0,unit='单')
            target=dict(profile='org:staff',groupId='cid-test',groupName='样例群',robotCode='robot',robotName='样例机器人')
            api=CommandAPI(service,CommandBusiness(Mock(),order=order,order_target=target),'robot','x'*40,org_id='org')
            grant={'member':dict(commands=list(COMMANDS),shops=['76'],fields=[])}
            for group in ('cid-test','cid-other'):
                api.groups.save('org','robot',group,list(COMMANDS),members=grant)
            actor=dict(org_id='org',group_id='cid-test',user_id='member')
            auth={'Authorization':'Bearer '+'x'*40}
            def request(ident,who):
                return api.route('POST','/api/commands/v1/requests',auth,dict(request_id=ident,actor=who,
                    command='order.match',args=dict(shop_id='76',order_number='NO-test')))
            self.assertEqual(request('dingtalk:other',dict(actor,group_id='cid-other'))[0],503)
            self.assertEqual(request('dingtalk:test',actor)[0],202)
            self.assertTrue(request('dingtalk:test',actor)[1]['replayed'])
            sender=Mock(return_value={'status':'unknown'})
            OrderDelivery(api,target,sender=sender).tick()
            OrderDelivery(api,target,sender=sender).tick()
            sender.assert_called_once()
            self.assertEqual(sender.call_args.args[0]['group_id'],'cid-test')
            self.assertIn('已匹配并回读确认',sender.call_args.args[1]['text']['content'])
            order.action.assert_called_once()
            status=api.status(('org','cid-test','member'),'dingtalk:test')
            self.assertEqual(status['delivery_status'],'unknown')
            self.assertNotIn('created_at',status['data'])

    def test_front_and_shipping_keep_exact_scopes_and_snapshot_limits(self):
        store,other='a'*32,'b'*32
        scopes={'front_stock':{'76':store,'32':other},'shipping_orders':{'76':'甲','32':'乙'},
                'shipping_shortages':{'76':'甲','32':'乙'}}
        bindings={k:dict(table_id=k,base_id='base',profile='org:staff',shops=scopes[k],
                         fields={f:f for f in sorted(fields)}) for k,fields in FIELDS.items()}
        rows={
            'shipping_orders':[dict(key='76:NO-1',urgency='已超时',checked='2026-11-05 10:00:00'),
                               dict(key='32:NO-2',urgency='紧急',checked='2026-11-05 10:00:00')],
            'shipping_shortages':[dict(key='42',stores='甲、乙',status='缺货',goods_id='42',
                                      gap='10',physical_gap='12',name='样例件',checked='2026-11-05 10:00:00')],
            'front_stock':[dict(key=f'{ident}:123:456',spu='123',sku_id='456',platform_stock='0',
                                replenish_state=dict(name='低库存'),snapshot_at='2026-11-06 10:00:00')
                           for ident in (store,other)]}
        source=LiveReportTables({},bindings,'org',query=lambda cfg,q:[dict(recordId=str(i),cells=r)
                                                for i,r in enumerate(rows[cfg['table_id']])])
        front=source.front('76',{'sku_id':'456'})
        self.assertEqual(front['counts']['total'],1)
        self.assertEqual(front['items'][0]['platform_stock'],0)
        ship=source.shipping({'76'})
        self.assertEqual(ship['tables'][0]['counts']['overdue'],1)
        self.assertIsNone(ship['tables'][0]['counts']['pending_scanned'])
        self.assertIsNone(ship['tables'][1]['counts']['shortage_goods'])
        full=source.shipping({'76','32'})
        self.assertEqual(full['tables'][1]['shortage_items'][0]['shortage'],12)
        self.assertEqual(full['outcome'],'partial')


if __name__=='__main__':
    unittest.main()
