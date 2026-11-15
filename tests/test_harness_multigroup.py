"""多群桥接的身份核验与固定指令入口顺序；不接触真实连接。"""
import hashlib
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import Mock, patch

CONTROL=Path(__file__).resolve().parents[1]/'integrations/order_backend/control'

class MultiGroupTests(unittest.TestCase):
    def test_fixed_route_precedes_ai_group_gate(self):
        module=runpy.run_path(str(CONTROL/'harness_runtime.py'))
        fn=module['patched'];g=fn.__globals__
        source=g['LOAD']+'            streamStatus = status;\n'+g['ADMISSION']+"fixture);\n        onMessage: async (msg) => {\n            const access = owner.authorize(msg);\n"+g['ROUTE']
        with patch.dict(g,KNOWN={hashlib.sha256(source.encode()).hexdigest()}):
            result=fn(source)
        self.assertEqual(result.count('stockRoute.isFixed(msg.text)'),1)
        self.assertLess(result.index('stockRoute.isFixed(msg.text)'),result.index('msg.conversationId !== process.env.STOCK_OPS_GROUP'))
        self.assertLess(result.index('msg.conversationId !== process.env.STOCK_OPS_GROUP'),result.index('owner.authorize(msg)'))

    def test_bridge_new_group_requires_member_probe(self):
        with patch.object(sys,'path',[str(CONTROL/'ops-bridge'),str(CONTROL/'ops-bridge/src'),*sys.path]):
            module=runpy.run_path(str(CONTROL/'harness_bridge.py'))
        fn=module['dispatch'];client=Mock();client.request.return_value={'member':True};execute=Mock(return_value={'Text':'fixture'})
        event=dict(Org='org',Robot='robot',Group='new-group',User='member',Message='message',Text='帮助')
        with patch.dict('os.environ',STOCK_OPS_WORKSPACE='synthetic',STOCK_OPS_GROUP='original'), patch.dict(fn.__globals__,connection=Mock(return_value=('org','robot',client)),run=execute):
            self.assertEqual(fn(dict(Event=event,State='prepare')),{'Text':'fixture'})
            client.request.assert_called_once_with('member',actor={'org_id':'org','group_id':'new-group','user_id':'member'})
            execute.reset_mock();client.request.return_value={'member':False}
            self.assertEqual(fn(dict(Event=event,State='prepare')),{});execute.assert_not_called()
            client.request.reset_mock()
            self.assertEqual(fn(dict(Event=dict(event,Robot='wrong'),State='prepare')),{});client.request.assert_not_called()

    def test_shipping_target_is_read_from_settings_each_time(self):
        # 只用临时队列验证目标选择，模拟发送器不连接钉钉。
        import sqlite3, tempfile, types
        from contextlib import closing
        with tempfile.TemporaryDirectory() as temp:
            dbpath=Path(temp)/'jobs.sqlite3'
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            bot=types.SimpleNamespace(queue_db=lambda root:sqlite3.connect(dbpath))
            robot=types.SimpleNamespace(get_config=lambda root:dict(groupId='configured-default',intervalMinutes=30),validate=lambda target:target)
            with patch.dict(sys.modules,group_order_bot=bot,robot_config=robot):
                module=runpy.run_path(str(CONTROL.parent/'matching/shipping_monitor.py'))
                self.assertEqual(module['config'](temp)['target']['groupId'],'configured-default')
                with closing(sqlite3.connect(dbpath)) as db, db:
                    db.execute("INSERT INTO settings VALUES('shippingTarget',?)",('{"groupId":"configured-shipping","intervalMinutes":60}',))
                value=module['config'](temp)
                self.assertEqual(value['target']['groupId'],'configured-shipping');self.assertFalse(value['inherit'])
