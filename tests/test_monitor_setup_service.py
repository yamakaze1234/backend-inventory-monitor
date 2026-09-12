import unittest
from monitor_setup_service import SetupService, SetupError, ChoiceRequired, validate_webhook, DEFAULT_ROWS
from monitor_secret_store import protect, unprotect


class SetupTests(unittest.TestCase):
    def test_recipient_template_keeps_four_name_only_sources(self):
        self.assertEqual(len(DEFAULT_ROWS), 4)
        self.assertEqual(sum(r['SenderName'] == '示例采购员' for r in DEFAULT_ROWS), 2)
        self.assertTrue(all('GroupId' not in r and 'SenderId' not in r for r in DEFAULT_ROWS))

    def test_webhook_validation_restricts_destination(self):
        validate_webhook('https://oapi.dingtalk.com/robot/send?access_token=example')
        for value in ['http://oapi.dingtalk.com/robot/send?access_token=x',
                      'https://evil.example/robot/send?access_token=x',
                      'https://oapi.dingtalk.com/robot/send',
                      'https://u@oapi.dingtalk.com/robot/send?access_token=x']:
            with self.assertRaises(SetupError):
                validate_webhook(value)

    def test_dpapi_roundtrip_does_not_store_plaintext(self):
        original = b'synthetic-test-value'
        blob = protect(original)
        self.assertNotIn(original, blob)
        self.assertEqual(unprotect(blob), original)

    def test_resolver_pins_profile_and_keeps_person_in_two_groups(self):
        service = SetupService('.')
        calls = []
        def cli(args, profile=None, **kwargs):
            calls.append(profile)
            if '+chat-search' in args:
                group = args[args.index('--query') + 1]
                return {'complete': True, 'chats': [{'title': group, 'openConversationId': 'id-' + group}]}
            return {'complete': True, 'users': [{'name': '示例采购员', 'openDingtalkId': 'person'}]}
        service.cli = cli
        rows = [dict(GroupName=g, SenderName='示例采购员', Terms='库存', AllowReopen=False) for g in ['旧群', '新群']]
        result = service.resolve_sources(rows, 'corp:user')
        self.assertEqual(len(result), 2)
        self.assertEqual(len({r['State'] for r in result}), 2)
        self.assertTrue(all(p == 'corp:user' for p in calls))

    def test_ambiguous_group_needs_selection(self):
        service = SetupService('.')
        service.cli = lambda *a, **kw: {'complete': True, 'chats': [
            {'title': '同名群', 'openConversationId': 'a'}, {'title': '同名群', 'openConversationId': 'b'}]}
        with self.assertRaises(ChoiceRequired):
            service.resolve_sources([dict(GroupName='同名群', SenderName='甲', Terms='库存')], 'c:u')

    def test_incomplete_search_fails_closed(self):
        service = SetupService('.')
        service.cli = lambda *a, **kw: {'complete': False, 'chats': [{'title': '群', 'openConversationId': 'a'}]}
        with self.assertRaises(SetupError):
            service.resolve_sources([dict(GroupName='群', SenderName='人', Terms='库存')], 'c:u')


if __name__ == '__main__':
    unittest.main()
