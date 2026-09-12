import json
import unittest

from suhao_cost_monitor import build_payload, should_forward


class SuhaoCostMonitorTests(unittest.TestCase):
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
