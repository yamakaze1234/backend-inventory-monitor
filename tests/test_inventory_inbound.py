import tempfile
import unittest
from inventory_monitor import InventoryService


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s=InventoryService(self.tmp.name)
        self.s.save_product(dict(sku='A',name='产品',category='内存',threshold=10))

    def snapshot(self, ident, journals, stock=20):
        return self.s.accept_snapshot(ident,[dict(sku='A',name='产品',able=20,purchase=0,stock=stock)],
            scope='test:2', journals={'A':journals})

    def row(self, ident='entry1', kind='入库单'):
        return dict(id=ident, sku='A', warehouse='公司大库', bill_code='RK-TEST',
                    kind=kind, in_qty=5, created_at=1000)

    def test_baseline_then_new_inbound_is_sent_once(self):
        self.snapshot('1',[self.row()])
        self.assertEqual(len(self.s.status()['events']),0)
        self.snapshot('2',[self.row(),self.row('entry2')],stock=25)
        self.snapshot('3',[self.row(),self.row('entry2')],stock=25)
        events=self.s.status()['events']
        self.assertEqual([e['kind'] for e in events],['inbound'])
        self.assertEqual(events[0]['after']['inbound']['in_qty'],5)
        self.assertTrue(self.s.status()['inbound_verified'])

    def test_stock_increase_alerts_even_with_empty_ledger(self):
        self.snapshot('1', [])
        self.snapshot('2', [], stock=25)
        self.snapshot('3', [], stock=25)
        self.assertEqual([e['kind'] for e in self.s.status()['events']], ['stock_increase'])

    def test_transfer_is_not_inbound(self):
        self.snapshot('1',[])
        self.snapshot('2',[self.row(kind='调拨单')])
        self.assertEqual(len(self.s.status()['events']),0)

    def test_bad_or_missing_journal_does_not_advance_snapshot(self):
        self.snapshot('1',[])
        with self.assertRaises(ValueError): self.snapshot('2',[dict(self.row(),warehouse='其他库')])
        with self.assertRaises(ValueError):
            self.s.accept_snapshot('3',[dict(sku='A',able=2,purchase=0,stock=2)],scope='test:2',journals={})
        self.assertEqual(self.s.status()['products'][0]['able'],20)

if __name__=='__main__': unittest.main()
