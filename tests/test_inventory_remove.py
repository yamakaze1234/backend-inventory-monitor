import json
import tempfile
import unittest
from unittest.mock import patch

from inventory_monitor import InventoryService


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = InventoryService(self.temp.name)
        for sku in ('A', 'B', 'C'):
            self.service.save_product(dict(sku=sku, name='商品 ' + sku, goods_id=sku, threshold=10))
        self.expected = {p['sku']: p for p in self.service.products()}

    def test_batch_removes_only_checked_and_preserves_history(self):
        with self.service.db() as db:
            for index, (sku, status) in enumerate([('A', 'pending'), ('B', 'failed'), ('B', 'sending'), ('C', 'pending'), ('A', 'sent')]):
                db.execute('INSERT INTO events(id,data,created,status) VALUES (?,?,?,?)',
                           (str(index), json.dumps(dict(sku=sku)), 1, status))
            db.execute('INSERT INTO snapshots VALUES (?,?)', ('A', '{}'))
        result = self.service.remove_products(['A', 'B', 'A'], expected=self.expected)
        self.assertEqual([p['sku'] for p in self.service.products()], ['C'])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]['in_flight'], 1)
        with self.service.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT status FROM events ORDER BY id')],
                             ['cancelled', 'cancelled', 'sending', 'pending', 'sent'])
            self.assertEqual(self.service.get(db, 'removed_product:A')['product'], self.expected['A'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)

    def test_modified_or_missing_selected_product_rejects_whole_batch(self):
        self.service.save_product(dict(self.expected['B'], threshold=22))
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.service.remove_products(['A', 'B'], expected=self.expected)
        self.assertEqual(len(self.service.products()), 3)
        self.service.remove_product('B')
        with self.assertRaises(ValueError):
            self.service.remove_products(['A', 'B'], expected=self.expected)
        self.assertIn('A', [p['sku'] for p in self.service.products()])

    def test_new_product_after_review_is_not_implicitly_deleted(self):
        self.service.save_product(dict(sku='D', name='新产品', threshold=5))
        self.service.remove_products(list(self.expected), expected=self.expected)
        self.assertEqual([p['sku'] for p in self.service.products()], ['D'])

    def test_mid_batch_error_rolls_back(self):
        original = self.service.put
        def put(db, key, value):
            if key == 'removed_product:B':
                raise ValueError('模拟保存失败')
            original(db, key, value)
        with patch.object(self.service, 'put', side_effect=put):
            with self.assertRaises(ValueError):
                self.service.remove_products(['A', 'B'], expected=self.expected)
        self.assertEqual(len(self.service.products()), 3)

    def test_empty_selection_changes_nothing(self):
        self.assertEqual(self.service.remove_products([]), [])
        self.assertEqual(len(self.service.products()), 3)


class RemoveUITests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from inventory_remove_ui import RemoveWindow
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = InventoryService(self.temp.name)
        for sku in ('A', 'B', 'C'):
            self.service.save_product(dict(sku=sku, goods_id=sku, name='商品 ' + sku, threshold=10))
        self.window = RemoveWindow(self.root, self.service)
        self.root.update()

    def test_open_and_cancel_do_not_delete(self):
        window = self.window
        self.assertEqual(window.checked, set())
        self.assertEqual(str(window.delete_button.cget('state')), 'disabled')
        window.select_all()
        self.assertEqual(len(window.checked), 3)
        with patch('inventory_remove_ui.messagebox.askyesno', return_value=False):
            self.assertFalse(window.confirm())
        self.assertEqual(len(self.service.products()), 3)
        window.clear_all()
        self.assertEqual(window.checked, set())

    def test_mouse_checkbox_space_all_and_delete(self):
        window = self.window
        x, y, width, height = window.tree.bbox('A', 'checked')
        window.tree.event_generate('<Button-1>', x=x+width//2, y=y+height//2)
        self.root.update()
        self.assertEqual(window.checked, {'A'})
        window.tree.selection_set('B')
        window.space_check()
        self.assertEqual(window.checked, {'A', 'B'})
        with patch('inventory_remove_ui.messagebox.askyesno', return_value=True):
            self.assertTrue(window.confirm())
        self.assertEqual(list(window.products), ['C'])
        window.select_all()
        with patch('inventory_remove_ui.messagebox.askyesno', return_value=True):
            self.assertTrue(window.confirm())
        self.assertEqual(self.service.products(), [])
        self.assertEqual(str(window.delete_button.cget('state')), 'disabled')

    def test_small_window_keeps_delete_button_visible(self):
        self.window.geometry('800x480')
        self.root.update()
        button = self.window.delete_button
        self.assertTrue(button.winfo_viewable())
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(), self.window.winfo_rooty()+self.window.winfo_height())


if __name__ == '__main__':
    unittest.main()
