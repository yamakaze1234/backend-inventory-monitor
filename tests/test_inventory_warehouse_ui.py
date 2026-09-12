import unittest
from inventory_panel import ProductDraft, product_risk_label
from inventory_delivery import format_events, format_inventory_report

class Service:
    def products(self): return []
    def related_messages(self, *args, **kwargs): return []

class WarehousePresentationTests(unittest.TestCase):
    def product(self, **changes):
        p = dict(sku='S', goods_id='G', name='显卡', threshold=25, enabled=True,
                 stock_basis='warehouse_stock', warehouse_id='W', warehouse_name='稀缺货源',
                 monitor_qty=20, warehouse_stock=20, warehouse_able=None, warehouse_purchase=0,
                 able=None, stock=21, purchase=0, risk='low')
        return dict(p, **changes)

    def test_draft_roundtrip_and_legacy_default(self):
        payload = ProductDraft.from_product(self.product()).payload()
        self.assertEqual(payload.get('stock_basis'), 'warehouse_stock')
        self.assertEqual(payload.get('warehouse_id'), 'W')
        self.assertEqual(payload.get('warehouse_name'), '稀缺货源')
        self.assertEqual(ProductDraft(threshold='3').payload().get('stock_basis'), 'company_able')

    def test_risk_uses_monitor_quantity_and_negative_stock(self):
        self.assertEqual(product_risk_label(self.product(), 'online'), ('低库存', 'low'))
        self.assertEqual(product_risk_label(self.product(monitor_qty=-1, risk='negative_stock'), 'online'), ('负库存', 'negative_stock'))
        self.assertEqual(product_risk_label(self.product(monitor_qty=None), 'online'), ('待检查', 'unknown'))

    def test_warehouse_report_is_not_company_available(self):
        body = format_inventory_report(dict(products=[self.product()], checked_at=1))
        self.assertIn('稀缺货源', body)
        self.assertIn('库存：20', body)
        self.assertNotIn('可销库存：20', body)
        self.assertNotIn('# 公司大库', body)

    def test_negative_recovery_and_previous_quantity(self):
        event = dict(kind='recovery', sku='S', name='显卡', threshold=25, observed_at=1,
                     after=self.product(), before=self.product(monitor_qty=-1, warehouse_stock=-1))
        body = format_events([event], Service(), now=2)
        self.assertIn('负库存解除，仍低库存', body)
        self.assertIn('稀缺货源', body)
        self.assertIn('上次分库库存：-1', body)
        self.assertNotIn('超售', body)

    def test_zero_and_negative_event_wording(self):
        for kind, qty, label in [('out_of_stock', 0, '分库库存已降至 0'), ('negative_stock', -1, '负库存')]:
            event = dict(kind=kind, sku='S', name='显卡', threshold=25, observed_at=1,
                         after=self.product(monitor_qty=qty, warehouse_stock=qty))
            body = format_events([event], Service(), now=2)
            self.assertIn(label, body)
            self.assertNotIn('可销库存', body)


class WarehouseControlTests(unittest.TestCase):
    def test_ready_lists_sample_without_auto_select_and_preserves_selection(self):
        import tkinter as tk
        from inventory_panel import InventoryPanel
        class Widget:
            def configure(self, **kw): self.options = kw
        class Lookup:
            def warehouse_options(self, goods_id):
                return dict(status='ready', depots=[dict(id='131', name='稀缺货源', stock=20), dict(id='125', name='样品库', stock=1)])
        panel = object.__new__(InventoryPanel)
        interp = tk.Tcl()
        panel.vars = {k: tk.StringVar(interp, v) for k,v in dict(sku='S', goods_id='88102', stock_basis='warehouse_stock', warehouse_id='', warehouse_name='').items()}
        panel.basis_choice = tk.StringVar(interp)
        panel.warehouse_choice = tk.StringVar(interp)
        panel.warehouse_state = tk.StringVar(interp)
        panel.warehouse_combo = Widget()
        panel.service = Lookup()
        self.assertTrue(hasattr(panel, 'refresh_warehouses'))
        panel.refresh_warehouses()
        self.assertEqual(panel.warehouse_choice.get(), '')
        self.assertTrue(any('样品库' in x for x in panel.warehouse_combo.options['values']))
        panel.warehouse_choice.set(panel.warehouse_combo.options['values'][0])
        panel.choose_warehouse()
        self.assertEqual(panel.vars['warehouse_id'].get(), '131')
        panel.refresh_warehouses()
        self.assertEqual(panel.vars['warehouse_name'].get(), '稀缺货源')

class WarehouseNullDisplayTests(unittest.TestCase):
    product = WarehousePresentationTests.product
    def test_null_warehouse_quantities_are_unknown(self):
        event = dict(kind='pending_inbound', sku='S', name='显卡', threshold=25, observed_at=1,
                     after=self.product(warehouse_purchase=None, purchase=99),
                     before=self.product(warehouse_purchase=None, purchase=88))
        body = format_events([event], Service(), now=2)
        self.assertNotIn('None', body)
        self.assertNotIn('99', body)
        self.assertNotIn('88', body)


if __name__ == '__main__': unittest.main()
