import json
import unittest

from suhao_cost_monitor import build_payload, should_forward


class SuhaoCostMonitorTests(unittest.TestCase):
    def test_reopen_title_requires_suhao_and_reopen_content(self):
        for sender in ('示例供货员', '示例业务员', '示例运营员', ''):
            for content in ('重开成本表', '重新开表', '成本表重新开', '成本重新开', '成本没调整，主要是发库存数量给大家参考', '已到货', '价格更新'):
                with self.subTest(sender=sender, content=content):
                    payload = build_payload(dict(sender=sender, text=content, messageId='private-id-123'))
                    expected = sender == '示例供货员' and content in ('重开成本表', '重新开表', '成本表重新开', '成本重新开')
                    self.assertEqual('重开成本表' in payload['markdown']['title'], expected)
                    title = '价格调整｜重开成本表' if expected else '货源情况'
                    self.assertEqual(payload['markdown']['title'], title)
                    self.assertTrue(payload['markdown']['text'].startswith(f'# {title}\n'))
                    self.assertNotIn('private-id-123', json.dumps(payload))
                    self.assertNotIn('源消息ID', payload['markdown']['text'])

    def test_configured_cost_source_uses_actual_recipient_sender(self):
        for source, content, expected in [(True, '重开成本表', '价格调整｜重开成本表'),
                                          (False, '重开成本表', '货源情况'),
                                          (True, '已经到货', '货源情况')]:
            payload = build_payload(dict(sender='自定义来源人员', cost_table_source=source, text=content))
            self.assertEqual(payload['markdown']['title'], expected)

    def test_tang_arrival_message_is_supply_information(self):
        text = '@所有人 Z890 AORUS MASTER AI TOP 已经到货'
        payload = build_payload(dict(sender='示例运营员', conversationName='示例运营群', text=text))
        self.assertEqual(payload['markdown']['title'], '货源情况')
        self.assertNotIn('价格调整', payload['markdown']['text'])
        self.assertIn(text, payload['markdown']['text'])

    def test_forwards_only_target_sender_group_and_cost_table_text(self):
        event = {
            "conversationId": "target-group",
            "senderId": "target-user",
            "messageId": "m-1",
            "text": "重新开表，AMD cpu价格更新",
        }
        self.assertTrue(
            should_forward(
                event,
                group_id="target-group",
                sender_id="target-user",
                terms=("重开", "成本表", "重新开", "调价", "降价"),
            )
        )

    def test_rejects_other_sender_and_unrelated_text(self):
        event = {
            "conversationId": "target-group",
            "senderId": "other-user",
            "messageId": "m-2",
            "text": "今天下午开会",
        }
        self.assertFalse(
            should_forward(
                event,
                group_id="target-group",
                sender_id="target-user",
                terms=("重开", "成本表", "重新开", "调价", "降价"),
            )
        )

    def test_payload_contains_keyword_and_source_message(self):
        payload = build_payload(
            {
                "conversationId": "target-group",
                "sender": "示例供货员",
                "createTime": "2026-09-08 14:05:18",
                "messageId": "m-3",
                "text": "重新开表 先按4300核算",
            }
        )
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertIn("价格调整", payload["markdown"]["title"])
        self.assertIn("重新开表 先按4300核算", payload["markdown"]["text"])
        json.dumps(payload, ensure_ascii=False)

    def test_forwards_a_configured_additional_source(self):
        event = {
            "conversationId": "group-peng",
            "senderId": "user-peng",
            "messageId": "m-4",
            "text": "库存越来越少，提货比例需要调整",
        }
        self.assertTrue(
            should_forward(
                event,
                sources=(("group-peng", "user-peng"),),
                terms=("库存", "提货", "调整"),
            )
        )

    def test_additional_source_cost_table_reopen_is_not_forwarded(self):
        event = {
            "conversationId": "group-peng",
            "senderId": "user-peng",
            "messageId": "m-5",
            "text": "重新开表，成本调价",
        }
        self.assertFalse(
            should_forward(
                event,
                sources=(("group-peng", "user-peng"),),
                terms=("缺货", "库存", "价格", "调价", "提货", "到货"),
                allow_reopen=False,
            )
        )

    def test_suhao_unrelated_arrival_message_is_not_forwarded(self):
        event = {
            "conversationId": "group-suhao",
            "senderId": "user-suhao",
            "messageId": "m-6",
            "text": "做下延迟，大概3天内到货",
        }
        self.assertFalse(
            should_forward(
                event,
                sources=(("group-suhao", "user-suhao"),),
                terms=("价格", "调价", "降价", "重开成本表", "重新开表", "成本表"),
            )
        )


if __name__ == "__main__":
    unittest.main()
