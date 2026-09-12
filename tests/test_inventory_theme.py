"""Offline interaction checks for the shared scrollbar."""
import tkinter as tk
import unittest


class ScrollbarTests(unittest.TestCase):
    def setUp(self):
        from inventory_theme import ScrollBar, install_theme
        self.root = tk.Tk()
        self.root.geometry('220x320+20+20')
        install_theme(self.root)
        self.calls = []
        self.bar = ScrollBar(self.root, command=lambda *args: self.calls.append(args))
        self.bar.pack(side='right', fill='y')
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_drag_starts_from_current_view_and_reaches_bottom(self):
        self.bar.set(.3, .5)
        self.root.update()
        start, end = self.bar.thumb_bounds()
        y = int((start + end) / 2)
        self.bar.event_generate('<ButtonPress-1>', x=6, y=y)
        self.bar.event_generate('<B1-Motion>', x=6, y=y)
        self.assertAlmostEqual(self.calls[-1][1], .3, places=2)
        self.bar.event_generate('<B1-Motion>', x=6, y=1000)
        self.assertAlmostEqual(self.calls[-1][1], .8)

    def test_track_keyboard_and_no_overflow(self):
        self.bar.set(0, .2)
        self.bar.event_generate('<ButtonPress-1>', x=6, y=270)
        self.assertEqual(self.calls[-1], ('scroll', 1, 'pages'))
        self.calls.clear()
        self.bar.set(0, 1)
        self.bar.event_generate('<ButtonPress-1>', x=6, y=270)
        self.assertFalse(self.calls)

    def test_theme_is_idempotent(self):
        from tkinter import ttk
        from inventory_theme import install_theme
        style = ttk.Style(self.root)
        style.configure('Inventory.Treeview', rowheight=54)
        install_theme(self.root)
        self.assertEqual(style.lookup('Inventory.Treeview', 'rowheight'), 54)

    def test_canvas_small_wheel_input_is_proportional(self):
        from inventory_theme import bind_wheel
        canvas = tk.Canvas(self.root, scrollregion=(0, 0, 200, 4000))
        canvas.pack(fill='both', expand=True)
        bind_wheel(canvas)
        self.root.update()
        canvas.yview_moveto(.3)
        before = canvas.canvasy(0)
        canvas.event_generate('<MouseWheel>', delta=-15)
        self.root.update()
        self.assertGreater(canvas.canvasy(0) - before, 0)
        self.assertLessEqual(canvas.canvasy(0) - before, 6)

    def test_scrollbar_reuses_thumb_on_scroll(self):
        self.bar.set(0, .2)
        original = self.bar.find_all()
        self.bar.set(.1, .3)
        self.assertEqual(original, self.bar.find_all())

    def test_tree_wheel_moves_one_row_and_retains_small_deltas(self):
        from tkinter import ttk
        tree = ttk.Treeview(self.root)
        tree.pack(fill='both', expand=True)
        for i in range(100):
            tree.insert('', 'end', text=str(i))
        self.root.update()
        tree.yview_moveto(0)
        tree.event_generate('<MouseWheel>', delta=-120)
        self.assertAlmostEqual(tree.yview()[0], .01)
        for _ in range(8):
            tree.event_generate('<MouseWheel>', delta=-15)
        self.assertAlmostEqual(tree.yview()[0], .02)

    def test_tree_refresh_preserves_selection_and_does_no_unchanged_writes(self):
        from inventory_theme import sync_tree
        from tkinter import ttk
        from unittest.mock import patch
        tree = ttk.Treeview(self.root, columns=('name',), show='headings')
        rows = [('a', ('Alpha',), ()), ('b', ('Beta',), ())]
        sync_tree(tree, rows)
        tree.selection_set('b')
        with patch.object(tree, 'item', wraps=tree.item) as item, patch.object(tree, 'move', wraps=tree.move) as move:
            sync_tree(tree, rows)
            self.assertFalse(any(call.kwargs for call in item.call_args_list))
            move.assert_not_called()
        sync_tree(tree, [('b', ('Changed',), ()), ('c', ('New',), ())])
        self.assertEqual(tree.selection(), ('b',))
        self.assertEqual(tree.get_children(), ('b', 'c'))

    def test_rounded_button_disabled_invoke_and_reenable(self):
        from inventory_theme import button
        calls = []
        control = button(self.root, '保存', lambda: calls.append('saved'), primary=True)
        control.configure(state='disabled')
        control.invoke()
        self.assertEqual(calls, [])
        self.assertEqual(control.cget('state'), 'disabled')
        control.configure(text='保存配置', state='normal')
        control.invoke()
        self.assertEqual(calls, ['saved'])
        self.assertEqual(control.cget('text'), '保存配置')


if __name__ == '__main__':
    unittest.main()
