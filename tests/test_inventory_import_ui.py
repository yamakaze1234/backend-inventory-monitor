import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from inventory_import_ui import ImportWindow
from inventory_monitor import InventoryService


class ImportUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.service = InventoryService(self.temp.name)
        self.service.accept_snapshot('fixture', [
            dict(sku='A', goods_id='101', name='主板', able=8, stock=8, purchase=0),
            dict(sku='B', goods_id='102', name='主板', able=9, stock=9, purchase=0),
        ], scope='offline')
        self.path = Path(self.temp.name) / 'products.csv'
        self.window = ImportWindow(self.root, self.service)

    def load(self, body):
        self.path.write_text('goodsid,名称,预警值\n' + body, encoding='utf-8-sig')
        self.window.load_file(self.path)
        self.root.update()

    def test_confirm_is_the_only_write_and_unapplied_edits_disable_it(self):
        self.load('101,,10\n')
        window = self.window
        self.assertEqual(self.service.products(), [])
        window.tree.selection_set('0')
        self.root.update()
        window.threshold.set('20')
        self.assertFalse(window.confirm())
        window.apply_row()
        self.assertTrue(window.confirm())
        self.assertEqual(self.service.products()[0]['threshold'], 20)
        self.assertFalse(window.confirm())

    def test_choose_ambiguous_product_and_exclude_invalid_row(self):
        self.load(',主板,0\n999,,5\n')
        window = self.window
        self.assertEqual(str(window.confirm_button.cget('state')), 'disabled')
        window.tree.selection_set('0')
        self.root.update()
        window.candidates.current(1)
        window.apply_row()
        window.tree.selection_set('1')
        self.root.update()
        window.toggle_row()
        self.assertTrue(window.confirm())
        self.assertEqual(self.service.products()[0]['goods_id'], '102')

    def test_cancel_and_failed_new_file_do_not_import_old_preview(self):
        self.load('101,,10\n')
        self.window.load_file(Path(self.temp.name) / 'missing.xlsx')
        self.assertFalse(self.window.confirm())
        self.assertEqual(self.service.products(), [])
        self.load('101,,10\n')
        self.window.destroy()
        self.assertEqual(self.service.products(), [])

    def test_confirmation_stays_visible_at_small_window_size(self):
        self.root.deiconify()
        self.window.geometry('880x590')
        self.load('101,,10\n')
        button = self.window.confirm_button
        self.assertTrue(button.winfo_viewable())
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                             self.window.winfo_rooty() + self.window.winfo_height())


if __name__ == '__main__':
    unittest.main()
