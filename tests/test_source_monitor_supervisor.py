import unittest
from pathlib import Path
from unittest.mock import patch
from source_monitor_supervisor import Supervisor, has_dingtalk
from suhao_cost_monitor import should_forward, build_payload


class FakeWorker:
    def __init__(self, source):
        self.alive = False
        self.starts = 0

    def start(self):
        self.starts += 1
        self.alive = True

    def is_alive(self):
        return self.alive


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        # Synthetic sources keep regression tests independent of local accounts.
        fixture_root = Path(__file__).parent / 'fixtures'
        self.root_patch = patch('source_monitor_supervisor.ROOT', fixture_root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_windowless_start_does_not_require_stdin(self):
        import source_monitor_supervisor as module
        with patch.object(module.sys, 'stdin', None):
            sources = module.load_sources()
        self.assertEqual(len(sources), 4)
        self.assertTrue(all(s['GroupId'] and s['SenderId'] for s in sources))

    def test_sender_keeps_both_groups_with_independent_deduplication(self):
        from source_monitor_supervisor import load_sources
        sources = load_sources()
        sender_sources = [s for s in sources if s.get('SenderName') == '示例采购员']
        self.assertEqual({s['GroupName'] for s in sender_sources}, {'示例整机业务群', '示例销售协同群'})
        self.assertEqual(len({s['State'] for s in sources}), len(sources))

    def test_waits_then_starts_once_and_recovers_dead_worker(self):
        manager = Supervisor([{}] * 3, FakeWorker)
        manager.tick(False)
        self.assertEqual([w.starts for w in manager.workers], [0, 0, 0])
        manager.tick(True)
        manager.tick(True)
        self.assertEqual([w.starts for w in manager.workers], [1, 1, 1])
        manager.workers[1].alive = False
        manager.tick(True)
        self.assertEqual([w.starts for w in manager.workers], [1, 2, 1])

    def test_tasklist_matches_client_only(self):
        self.assertFalse(has_dingtalk('"dingpan_sync.exe","10"'))
        self.assertTrue(has_dingtalk('"DingTalk.exe","10"'))

    def test_flattened_dws_message_is_accepted_and_has_message_id(self):
        event = dict(conversation_id='g', sender_open_dingtalk_id='u',
                     message_id='m', content='库存到货', create_time='now')
        self.assertTrue(should_forward(event, group_id='g', sender_id='u'))
        self.assertIn('源消息ID：m', build_payload(event)['markdown']['text'])


if __name__ == '__main__':
    unittest.main()
