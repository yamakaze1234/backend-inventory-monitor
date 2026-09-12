"""Offline collection contract tests; FakeSetupService cannot execute DWS."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from inventory_weekly import TZ, collect_range


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start = datetime(2026, 9, 7, tzinfo=TZ)
        self.end = datetime(2026, 9, 14, tzinfo=TZ)
        self.sources = [dict(GroupId='g1', SenderId='s1', GroupName='来源群', SenderName='供货员', Profile='test-profile')]
        self.write_sources()
        self.result = dict(complete=True, hasMore=False, failures=[], failedCount=0,
                           scope=dict(resultsWithinScope=True), senderScope=dict(resultsWithinScope=True),
                           messages=[self.message()])
        self.calls = []
        owner = self
        class FakeSetupService:
            def __init__(self, root):
                owner.assertEqual(Path(root), owner.root)
            def cli(self, args, profile=None, timeout=None):
                owner.calls.append((args, profile, timeout))
                return copy.deepcopy(owner.result)
        self.patch = patch('monitor_setup_service.SetupService', FakeSetupService)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write_sources(self):
        (self.root / 'monitor_sources.json').write_text(json.dumps(self.sources), encoding='utf-8')

    def message(self, **changes):
        return dict(dict(conversationId='g1', senderId='s1', messageId='m1',
                         createTime='2026-09-11 14:05:56', text='5070 到货 20 片'), **changes)

    def collect(self):
        return collect_range(self.root, self.start, self.end)

    def test_empty_source_is_checked_twice(self):
        self.result['messages']=[]
        rows,_=self.collect()
        self.assertEqual(rows,[])
        self.assertEqual(len(self.calls),2)

    def test_queries_all_pages_with_explicit_range_profile_and_read_only_command(self):
        rows, sources = self.collect()
        args, profile, timeout = self.calls[0]
        self.assertEqual(args[:2], ['chat', '+search-msg'])
        self.assertEqual(args[args.index('--start') + 1], self.start.isoformat())
        self.assertEqual(args[args.index('--end') + 1], self.end.isoformat())
        self.assertIn('--page-all', args)
        self.assertEqual(profile, 'test-profile')
        self.assertEqual(rows[0]['created_at'], datetime(2026,9,11,14,5,56,tzinfo=TZ).timestamp())
        self.assertEqual(sources, self.sources)

    def test_rejects_each_incomplete_or_out_of_scope_result(self):
        cases = [dict(complete=False), dict(hasMore=True), dict(failures=['failed']),
                 dict(failedCount=1), dict(scope=dict(resultsWithinScope=False)),
                 dict(senderScope=dict(resultsWithinScope=False)), dict(messages=None)]
        original = copy.deepcopy(self.result)
        for change in cases:
            with self.subTest(change=change):
                self.result = dict(original, **change)
                with self.assertRaises(ValueError): self.collect()

    def test_includes_start_excludes_exact_end_and_sorts(self):
        self.result['messages'] = [self.message(),
            self.message(messageId='end', createTime=self.end.isoformat()),
            self.message(messageId='start', createTime=self.start.isoformat())]
        rows, _ = self.collect()
        self.assertEqual([r['message_id'] for r in rows], ['start', 'm1'])

    def test_rejects_messages_before_start_or_after_end(self):
        for stamp in (self.start - timedelta(seconds=1), self.end + timedelta(seconds=1)):
            with self.subTest(stamp=stamp):
                self.result['messages'] = [self.message(createTime=stamp.isoformat())]
                with self.assertRaises(ValueError): self.collect()

    def test_rejects_wrong_group_sender_or_missing_message_id(self):
        for change in (dict(conversationId='other'), dict(senderId='other'), dict(messageId='')):
            with self.subTest(change=change):
                self.result['messages'] = [self.message(**change)]
                with self.assertRaises(ValueError): self.collect()

    def test_identical_duplicate_is_deduplicated_in_and_across_sources(self):
        self.sources += copy.deepcopy(self.sources)
        self.write_sources()
        self.result['messages'] = [self.message(), self.message()]
        rows, _ = self.collect()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(rows), 1)

    def test_conflicting_duplicate_fails_closed(self):
        self.result['messages'] = [self.message(), self.message(text='5070 到货 99 片')]
        with self.assertRaisesRegex(ValueError, '重复消息'): self.collect()

    def test_missing_profile_rejected_before_any_query(self):
        self.sources[0].pop('Profile')
        self.write_sources()
        with self.assertRaises(ValueError): self.collect()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
