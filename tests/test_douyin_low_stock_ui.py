"""独立数据验证规则窗口保存，不操作真实应用或店铺。"""
import tempfile
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from douyin_inventory import DouyinInventory
from douyin_low_stock_ui import open_rules


class RuleWindowTests(unittest.TestCase):
    def test_save_disabled_component_and_default_threshold(self):
        base=Path(__file__).resolve().parents[1]/'.codex_tmp/low-stock-tests'
        base.mkdir(parents=True,exist_ok=True)
        store=DouyinInventory(Path(tempfile.mkdtemp(dir=base)))
        ident=store.add_store('离线测试店')
        root=tk.Tk();root.withdraw()
        owner=tk.Frame(root);owner.ident=ident;owner.notice=tk.StringVar(root)
        owner.runtime=SimpleNamespace(store=store,submit=Mock())
        owner.refresh_details=Mock()
        win=open_rules(owner);root.update()
        def controls(widget):
            for child in widget.winfo_children():
                yield child
                yield from controls(child)
        try:
            widgets=list(controls(win))
            for w in widgets:
                if w.winfo_class()=='Button':
                    self.assertTrue(w.winfo_ismapped())
                    self.assertLessEqual(w.winfo_rooty()+w.winfo_height(),win.winfo_rooty()+win.winfo_height())
            spins=[w for w in widgets if w.winfo_class()=='TSpinbox']
            spins[0].delete(0,'end');spins[0].insert(0,'2')
            combo=next(w for w in widgets if w.winfo_class()=='TCombobox');combo.set('12')
            next(w for w in widgets if w.winfo_class()=='TCheckbutton').invoke()
            next(w for w in widgets if w.winfo_class()=='Button' and w.cget('text')=='添加 / 修改规则').invoke()
            tree=next(w for w in widgets if w.winfo_class()=='Treeview')
            self.assertEqual(tree.item('12','values')[1],'不预警')
            next(w for w in widgets if w.winfo_class()=='Button' and w.cget('text')=='保存设置').invoke()
            self.assertEqual(store.low_stock_rules(ident),dict(default=2,parts={'12':None}))
            owner.runtime.submit.assert_called_once_with('online',ident)
        finally:
            root.destroy()
