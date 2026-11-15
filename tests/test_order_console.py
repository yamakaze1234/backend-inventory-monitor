"""整合回归：只用临时队列和模拟命令，不连接 ERP 或群消息。"""
import io
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

from inventory_console import InventoryConsole
from inventory_monitor import InventoryService
from order_bot_adapter import OrderBotAdapter


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        output=Path(__file__).resolve().parents[1]/'outputs/订单整合验收/tests'
        output.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=output))
        self.service=InventoryService(self.root/'inventory')
        self.bot=OrderBotAdapter(state_root=self.root/'orders')
        assets=self.root/'assets'
        assets.mkdir()
        (assets/'index.html').write_text('<!doctype html><title>隔离测试</title>',encoding='utf-8')
        self.console=InventoryConsole(self.service,bot=self.bot,assets=assets)

    def tearDown(self):
        self.console.close()
        # 本轮验收结束后统一移入回收站，不由 tempfile 永久删除目录。

    def test_read_does_not_create_order_database(self):
        self.assertFalse(self.bot.snapshot()['available'])
        self.assertFalse(self.bot.state_root.exists())

    def test_order_pagination_and_unknown_count(self):
        self.bot.state_root.mkdir()
        path=self.bot.state_root/'jobs.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript('CREATE TABLE settings(key TEXT,value TEXT); CREATE TABLE jobs(id TEXT,kind TEXT,number TEXT,shop TEXT,state TEXT,created REAL,delivery TEXT);')
            db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',[(str(i),'order',str(i),'76','unknown' if i==1 else 'verified',i,'pending') for i in range(30)])
            db.commit()
        before=path.read_bytes()
        result=self.bot.snapshot(1,25)
        self.assertEqual(30,result['total'])
        self.assertEqual(5,len(result['jobs']))
        self.assertEqual(1,result['active'])
        self.assertEqual(before,path.read_bytes())

    def request(self,path,body=None,token=True,origin=None):
        headers={'X-Console-Token':self.console.token} if token else {}
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(self.console.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        return urllib.request.urlopen(request)

    def test_cross_origin_and_missing_session_block_writes(self):
        for token,origin in ((False,None),(True,'https://example.com')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/inventory/enabled',{'enabled':False},token,origin)
            self.assertEqual(403,error.exception.code)
        self.assertTrue(self.service.status()['enabled'])

    def test_inventory_round_trip_and_no_connection_secrets(self):
        with self.request('/api/inventory/enabled',{'enabled':False}) as response:
            self.assertEqual(200,response.status)
        with self.request('/api/inventory') as response:
            result=json.load(response)
        self.assertFalse(result['enabled'])
        self.assertNotIn('destination',result)

    def test_arbitrary_command_rejected_before_runner(self):
        self.bot.runner=Mock()
        with self.assertRaises(ValueError):self.bot.action('shell',{'command':'anything'})
        self.bot.runner.assert_not_called()

    def test_update_install_blocked_for_active_orders(self):
        self.console.native_action=Mock()
        self.bot.snapshot=Mock(return_value={'active':1})
        with self.assertRaisesRegex(ValueError,'匹配任务'):
            self.console.action('/api/update/install',{})
        self.console.native_action.assert_not_called()

    def test_shutdown_blocks_new_start(self):
        self.console.closing=True
        self.bot.action=Mock()
        with self.assertRaisesRegex(ValueError,'安全退出'):
            self.console.action('/api/orders/action',{'action':'start'})
        self.bot.action.assert_not_called()

    def test_native_embedding_endpoint_is_removed(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/native',{'module':'monitor'})
        self.assertEqual(400,error.exception.code)

    def test_failed_restore_is_consumed_without_crashing_or_restarting_again(self):
        from inventory_app import restore_update_services
        controller=Mock()
        controller.start.side_effect=RuntimeError('模拟消息恢复失败')
        self.bot.action=Mock(side_effect=RuntimeError('模拟订单恢复失败'))
        with self.service.db() as db:
            for key in ('update_restore_order_bot','update_restore_messages'):
                self.service.put(db,key,True)
        restore_update_services(self.service,controller,self.console)
        self.assertIn('未恢复',self.console.updater.state['error'])
        restore_update_services(self.service,controller,self.console)
        self.assertEqual(1,controller.start.call_count)
        self.assertEqual(1,self.bot.action.call_count)


if __name__=='__main__':unittest.main()
