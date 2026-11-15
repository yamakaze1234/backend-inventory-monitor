"""Isolated source preview: no ERP connection, listener or notification sender."""
from project_paths import PROJECT_ROOT
from pathlib import Path
import tkinter as tk

from inventory_monitor import InventoryService
from test_inventory_ui_local import build_workspace
from douyin_inventory_runtime import DouyinRuntime


def main():
    service = InventoryService(PROJECT_ROOT / '.local_douyin_preview')
    root = tk.Tk()
    root.title('抖店库存整合预览 · 不发送通知')
    root.geometry('1380x900')
    root.minsize(1080, 680)
    runtime = DouyinRuntime(service, live=False)
    try:
        panel = build_workspace(root, service, live=False)
        panel.douyin_runtime = runtime
        panel.show_module('douyin')
        root.mainloop()
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
