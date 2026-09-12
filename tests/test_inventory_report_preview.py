import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from notification_robots import RobotStore
from inventory_report_delivery import destinations, send_reviewed, split_message

class ReportDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=RobotStore(self.temp.name)
        with patch('monitor_secret_store.protect',side_effect=lambda value:value):
            self.one=self.store.save('一号','https://oapi.dingtalk.com/robot/send?access_token=test-one',['messages'])
            self.two=self.store.save('二号','https://oapi.dingtalk.com/robot/send?access_token=test-two',['daily'])
        self.path=Path(self.temp.name)/'report.md'
        self.path.write_text('原文',encoding='utf-8')

    def test_selected_only_exact_text_and_persistent_dedup(self):
        target=destinations(self.store)[0]
        transport=Mock(return_value={'status':'sent'})
        for _ in range(2): result=send_reviewed(self.store,self.path,'修改后的报告',[target],transport)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(transport.call_args.args[1]['markdown']['text'],'修改后的报告')
        self.assertEqual(self.path.read_text('utf-8'),'修改后的报告')
        self.assertEqual(result[0]['status'],'sent')

    def test_changed_destination_rejected(self):
        target=destinations(self.store)[0]
        self.store.set_enabled(target['id'],False)
        transport=Mock()
        with self.assertRaises(ValueError):send_reviewed(self.store,self.path,'内容',[target],transport)
        transport.assert_not_called()

    def test_unknown_never_retried(self):
        transport=Mock(return_value={'status':'unknown'})
        target=destinations(self.store)[0]
        for _ in range(2):result=send_reviewed(self.store,self.path,'内容',[target],transport)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(result[0]['status'],'unknown')

    def test_chunks_preserve_all_bytes(self):
        body=('中文货源\n'*5000)
        chunks=split_message(body)
        self.assertEqual(''.join(chunks),body)
        self.assertTrue(all(len(c.encode('utf-8'))<=16000 for c in chunks))

    def test_partial_stops_remaining_chunks_and_does_not_repeat(self):
        body='中'*18000
        target=destinations(self.store)[0]
        transport=Mock(side_effect=[{'status':'sent'},{'status':'failed'}])
        result=send_reviewed(self.store,self.path,body,[target],transport)
        self.assertEqual(result[0]['status'],'partial')
        self.assertEqual(result[0]['sent_parts'],1)
        send_reviewed(self.store,self.path,body,[target],transport)
        self.assertEqual(transport.call_count,2)

    def test_transport_failure_is_unknown_and_receipt_is_durable(self):
        target=destinations(self.store)[0]
        transport=Mock(side_effect=RuntimeError('private'))
        result=send_reviewed(self.store,self.path,'文字',[target],transport)
        self.assertEqual(result[0]['status'],'unknown')
        self.assertNotIn('private',self.path.with_suffix('.delivery.json').read_text('utf-8'))

    def test_webhook_change_after_preview_is_rejected(self):
        target=destinations(self.store)[0]
        with patch('monitor_secret_store.protect',side_effect=lambda value:value):
            self.store.save(target['name'],'https://oapi.dingtalk.com/robot/send?access_token=changed',['messages'],robot_id=target['id'])
        with self.assertRaises(ValueError):send_reviewed(self.store,self.path,'文字',[target],Mock())
