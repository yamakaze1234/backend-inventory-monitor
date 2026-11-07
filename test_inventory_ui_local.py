"""Local ERP integration UI; accepts live collection, never starts a sender."""
from pathlib import Path
import ctypes
import tkinter as tk
import sqlite3
from tkinter import messagebox

from inventory_monitor import InventoryService
from inventory_panel import InventoryPanel
from inventory_bridge import start_bridge
from inventory_version import APP_TITLE


def start_test_bridge(service, port=18763):
    return start_bridge(service, port=port, live_delivery=False)


# 测试和正式运行使用相同的工作台，避免两套界面逐渐分叉。
from inventory_workspace import build_workspace


def main():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title('后台库存监控 · 本地源码测试 v0.9')
    root.geometry(f'{min(1380, root.winfo_screenwidth()-60)}x{min(900, root.winfo_screenheight()-90)}')
    root.minsize(1080, 680)
    root.option_add('*Font', ('Microsoft YaHei UI', 10))
    service = InventoryService(Path(__file__).parent / '.local_inventory_ui_test')
    source = Path(__file__).parent / 'inventory.sqlite3'
    if source.exists() and not service.catalog():
        # Only the previously collected product catalog and its timestamp are copied.
        # Personal configuration, watched products and delivery state stay untouched.
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as original:
            catalog = original.execute('SELECT sku,data FROM catalog').fetchall()
            checked = original.execute("SELECT data FROM meta WHERE key='checked_at'").fetchone()
        if catalog:
            with service.db() as db:
                db.execute('DELETE FROM catalog')
                db.executemany('INSERT INTO catalog VALUES (?,?)', catalog)
                if checked:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('checked_at', ?)", checked)
    try:
        bridge = start_test_bridge(service)
    except OSError:
        messagebox.showerror('ERP 采集连接未启动', '本机 18763 端口已被占用或不可用。请关闭其他库存监控窗口后重新打开。', parent=root)
        root.destroy()
        return
    panel = build_workspace(root, service)
    try:
        root.mainloop()
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
