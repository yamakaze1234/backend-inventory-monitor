"""企业机器人契约测试：只使用本地替身，不发送钉钉消息。"""
import subprocess
import unittest
from unittest.mock import patch
from notification_enterprise import EnterpriseClient, send_enterprise

TARGET=dict(profile='corp:user',group_id='cidfixture',group_name='样例群',robot_code='dingfixture',robot_name='样例机器人')
PAYLOAD=dict(msgtype='markdown',markdown=dict(title='测试标题',text='离线内容\n\n[明细](https://example.com)'))


class EnterpriseTest(unittest.TestCase):
    def test_bot_filter_and_target_membership(self):
        client=EnterpriseClient()
        data=dict(complete=True,conversationId='cidfixture',bots=[dict(name='普通',robotCode='normal'),dict(name='助手',robotCode='intelligent'),dict(name='企业',robotCode='dingfixture')])
        with patch.object(client,'call',return_value=data):
            self.assertEqual(client.verify(TARGET),TARGET)
            self.assertEqual(len(client.bots('corp:user','cidfixture')),1)
            with self.assertRaises(ValueError): client.verify({**TARGET,'robot_code':'dingother'})
        with patch.object(client,'call',return_value={**data,'complete':False}):
            with self.assertRaises(ValueError): client.bots('corp:user','cidfixture')

    def test_send_uses_bot_identity_explicit_profile_and_never_retries(self):
        success=dict(ok=True,result=dict(success=True,result=dict(processQueryKey='fixture')))
        with patch.object(EnterpriseClient,'verify',return_value=TARGET), patch.object(EnterpriseClient,'call',return_value=success) as call:
            self.assertEqual(send_enterprise(TARGET,PAYLOAD)['status'],'sent')
            args,profile=call.call_args.args
            self.assertEqual(profile,'corp:user'); self.assertIn('--as',args); self.assertIn('bot',args)
            self.assertIn(PAYLOAD['markdown']['text'],args); self.assertIn('cidfixture',args)
        with patch.object(EnterpriseClient,'verify',return_value=TARGET), patch.object(EnterpriseClient,'call',side_effect=subprocess.TimeoutExpired('dws',60)) as call:
            self.assertEqual(send_enterprise(TARGET,PAYLOAD),dict(status='unknown',retryable=False))
            call.assert_called_once()

    def test_validation_failure_never_sends(self):
        with patch.object(EnterpriseClient,'verify',side_effect=ValueError('机器人不在群中')), patch.object(EnterpriseClient,'call') as call:
            self.assertEqual(send_enterprise(TARGET,PAYLOAD),dict(status='failed',retryable=False))
            call.assert_not_called()

    def test_ambiguous_response_is_unknown(self):
        with patch.object(EnterpriseClient,'verify',return_value=TARGET), patch.object(EnterpriseClient,'call',return_value=dict(ok=True,result={})):
            self.assertEqual(send_enterprise(TARGET,PAYLOAD)['status'],'unknown')


    def test_price_text_is_sent_as_markdown_without_mutating_event(self):
        # 模拟价格监控实际生成的 text 事件，核对路由、内容和换行转换。
        content='ERP 单价累计变化达到 100 元 · goods_id 123\n配件：处理器\n公司大库库存成本：100.00 → 200.00 元'
        payload=dict(msgtype='text',text=dict(content=content))
        success=dict(ok=True,result=dict(success=True,result=dict(processQueryKey='fixture')))
        with patch.object(EnterpriseClient,'verify',return_value=TARGET), patch.object(EnterpriseClient,'call',return_value=success) as call:
            self.assertEqual(send_enterprise(TARGET,payload),dict(status='sent',retryable=False))
            args,profile=call.call_args.args
            self.assertEqual(profile,TARGET['profile'])
            self.assertEqual(args[args.index('--robot-code')+1],TARGET['robot_code'])
            self.assertEqual(args[args.index('--group')+1],TARGET['group_id'])
            self.assertEqual(args[args.index('--markdown')+1],content.replace('\n','  \n'))
            self.assertEqual(payload,dict(msgtype='text',text=dict(content=content)))

    def test_invalid_or_empty_text_never_sends(self):
        with patch.object(EnterpriseClient,'verify',return_value=TARGET), patch.object(EnterpriseClient,'call') as call:
            for payload in (dict(msgtype='text',text=dict(content='   ')),dict(msgtype='text',text=dict(content=123)),dict(msgtype='image'),dict(msgtype='markdown',markdown=dict(text=None))):
                self.assertEqual(send_enterprise(TARGET,payload),dict(status='failed',retryable=False))
            call.assert_not_called()
