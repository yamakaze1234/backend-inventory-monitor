"""部门订单后端隔离验证：只使用合成文件，不启动 ERP 或发送机器人消息。"""
import json
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import types
import tempfile
import unittest
from unittest.mock import patch
import order_bot_config as config
from order_bot_adapter import OrderBotAdapter


class DepartmentTests(unittest.TestCase):
    def setUp(self):
        output=Path(__file__).resolve().parents[1]/'outputs/order-department-tests'
        output.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=output))
        self.workspace=self.root/'workspace'
        self.match=self.root/'matching';self.control=self.root/'control';self.state=self.root/'state'
        (self.match/'app/src').mkdir(parents=True);self.control.mkdir()
        (self.match/'app/data').mkdir()
        (self.match/'app/data/维护表设置.json').write_text(json.dumps(dict(version=1,shops={key:dict(name='测试',path=str(self.root/'fixture.xlsx'),sheet='Sheet1') for key in ('987','988')})),encoding='utf-8')
        for file in [self.match/'app/src'/n for n in ('group_order_bot.py','patrol_worker.py','batch_match.py')]+[self.control/n for n in ('robot_desktop_control.py','robot_record_cleanup.py')]:
            file.write_text('# 合成后端\n',encoding='utf-8')
        binary=self.root/'runtime.exe';binary.write_bytes(b'fixture')
        self.values=dict(match_root=str(self.match),control_root=str(self.control),state_root=str(self.state),shops=[dict(id='987',name='测试部门')],**{key:str(binary) for key in ('python_path','dws_path','pwsh_path','node_path')})

    def test_unconfigured_read_has_no_side_effect(self):
        adapter=OrderBotAdapter(workspace_root=self.workspace)
        self.assertFalse(adapter.snapshot()['available'])
        self.assertEqual([],adapter.snapshot()['shops'])
        with self.assertRaises(ValueError):adapter.action('start')
        self.assertFalse(self.workspace.exists())

    def test_deploy_tracks_paths_and_keeps_originals(self):
        config.save(self.workspace,self.values)
        result=config.deploy(self.workspace)
        self.assertTrue(config.deployed(self.workspace))
        self.assertIn('合成后端',(Path(result['backup'])/'matching/group_order_bot.py').read_text(encoding='utf-8'))
        import tomllib
        agent=tomllib.loads((self.state/'agent/.codex/config.toml').read_text(encoding='utf-8'))
        self.assertEqual(str(config.config_path(self.workspace)),agent['mcp_servers']['erp_order_matching']['env']['ORDER_BOT_CONFIG'])
        (self.control/'order-department.json').write_text('{}')
        self.assertFalse(config.deployed(self.workspace))

    def test_reject_foreign_backend_and_existing_agent(self):
        config.save(self.workspace,self.values)
        (self.control/'order-department.json').write_text(json.dumps(dict(configPath=str(self.root/'other.json'))))
        with self.assertRaisesRegex(ValueError,'其他部门'):config.deploy(self.workspace)

    def test_shipping_readiness_detects_stale_sync_even_with_current_monitor(self):
        config.save(self.workspace,self.values)
        config.deploy(self.workspace)
        self.assertTrue(config.describe(self.workspace)['shippingBackendCurrent'])
        (self.match/'app/src/shipping_online.py').write_text('# 旧同步实现',encoding='utf-8')
        result=config.describe(self.workspace)
        self.assertTrue(result['shippingReady'])
        self.assertFalse(result['shippingBackendCurrent'])

    def test_unknown_delivery_blocks_install(self):
        config.save(self.workspace,self.values)
        self.state.mkdir()
        with sqlite3.connect(self.state/'jobs.sqlite3') as db:
            db.executescript("CREATE TABLE settings(key TEXT,value TEXT); CREATE TABLE jobs(state TEXT,delivery TEXT); INSERT INTO jobs VALUES('done','unknown');")
        with self.assertRaisesRegex(ValueError,'未确认'):config.deploy(self.workspace)

    def test_connector_unknown_or_running_blocks_changes(self):
        config.save(self.workspace,self.values);self.state.mkdir()
        with sqlite3.connect(self.state/'jobs.sqlite3') as db:
            db.executescript('CREATE TABLE settings(key TEXT,value TEXT); CREATE TABLE jobs(state TEXT,delivery TEXT);')
            db.execute('INSERT INTO settings VALUES(?,?)',('robotConfig',json.dumps(dict(robotCode='dingtest',profile='test-profile'))))
        # 在线、矛盾或不可用的回读均不能覆盖后端。
        for exit_code,payload in ((0,{'state':'healthy'}),(0,{'state':'degraded'}),
                                  (0,{'state':'unknown'}),(0,{'state':'not_running','pid':42}),
                                  (0,{'state':'not_running','error':'probe failed'}),
                                  (1,{'state':'not_running'}),(0,None)):
            with self.subTest(exit_code=exit_code,payload=payload), patch.object(
                    config.subprocess,'run',return_value=subprocess.CompletedProcess([],exit_code,json.dumps(payload),'')):
                with self.assertRaisesRegex(ValueError,'连接器'):config.deploy(self.workspace)
        with patch.object(config.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"state":"down"}','')):
            config.assert_connector_stopped(self.values)
        # 复制旧队列到从未运行过 DWS Stream 的新电脑，实测返回 not_running、无 PID。
        with patch.object(config.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"state":"not_running","supervised":false}','')):
            self.assertTrue(config.deploy(self.workspace)['changed'])

    def test_same_named_group_robot_with_different_code_is_rejected(self):
        stub=types.ModuleType('robot_config');stub.DEFAULT={};stub.get_config=lambda:None;stub.validate=lambda x:x
        path=config.bundle_root()/'control/robot_settings.py'
        spec=importlib.util.spec_from_file_location('isolated_robot_settings',path);module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,robot_config=stub):spec.loader.exec_module(module)
        def run(command,**kwargs):
            if command[1:3]==['profile','list']:value={'profiles':[dict(profile='p',isOrgCurrent=True)]}
            elif command[1:4]==['dev','app','get']:value={'data':dict(unifiedAppId='app',appStatus='published',appKey='dingexpected')}
            elif command[1:5]==['dev','app','robot','get']:value={'data':dict(unifiedAppId='app',configured=True,mode='STREAM',robotStatus='ONLINE',robotCode='dingexpected',name='同名机器人')}
            elif command[2]=='+chat-search':value=dict(complete=True,chats=[dict(openConversationId='cidtest',name='测试群')])
            else:value=dict(complete=True,conversationId='cidtest',bots=[dict(robotCode='dingdifferent',name='同名机器人')])
            return json.dumps(value)
        with self.assertRaisesRegex(ValueError,'未唯一找到'):
            module.validated_target(dict(appId='app',groupId='cidtest',groupName='测试群',intervalMinutes=120),run,'dws')

    def test_failure_restores_source(self):
        config.save(self.workspace,self.values)
        original=config.shutil.copy2
        def failing(source,target,*args,**kwargs):
            if str(source).endswith('robot_settings.py') and 'integrations' in str(source):raise OSError('模拟写入失败')
            return original(source,target,*args,**kwargs)
        with patch.object(config.shutil,'copy2',side_effect=failing):
            with self.assertRaises(OSError):config.deploy(self.workspace)
        self.assertIn('合成后端',(self.match/'app/src/group_order_bot.py').read_text(encoding='utf-8'))
        self.assertFalse(config.deployed(self.workspace))

    def test_changed_config_requires_new_deployment(self):
        config.save(self.workspace,self.values);config.deploy(self.workspace)
        config.save(self.workspace,dict(self.values,shops=[dict(id='988',name='另一店铺')]))
        self.assertFalse(config.deployed(self.workspace))

    def test_cannot_switch_deployed_workspace_to_other_department_state(self):
        config.save(self.workspace,self.values);config.deploy(self.workspace)
        other=self.root/'other-state';other.mkdir()
        (other/'order-owner.json').write_text(json.dumps(dict(configPath=str(self.root/'other-workspace/order-bot.json'))))
        with self.assertRaisesRegex(ValueError,'其他部门'):
            config.save(self.workspace,dict(self.values,state_root=str(other)))
        self.assertEqual(str(self.state),config.load(self.workspace)['state_root'])

    @unittest.skipUnless(shutil.which('node'),'需要 Node 执行 MCP 工具清单验证')
    def test_mcp_tools_use_department_shop_range(self):
        config.save(self.workspace,self.values);config.deploy(self.workspace)
        result=subprocess.run(['node',str(self.control/'order-match-server.mjs')],input=json.dumps(dict(jsonrpc='2.0',id=1,method='tools/list'))+'\n',text=True,capture_output=True,encoding='utf-8',timeout=10,env=dict(os.environ,ORDER_BOT_CONFIG=str(config.config_path(self.workspace))))
        self.assertEqual(0,result.returncode,result.stderr)
        tools=json.loads(result.stdout)['result']['tools']
        for tool in tools:
            shop=tool['inputSchema']['properties'].get('shopId')
            if shop:self.assertEqual(['987'],shop['enum'])
