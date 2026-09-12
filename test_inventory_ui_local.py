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


def build_workspace(root, service, live=False, dingtalk_controller=None):
    from tkinter import ttk
    from inventory_panel import BG, INK, MUTED, BLUE
    from inventory_theme import install_theme
    install_theme(root)
    sidebar = tk.Frame(root, bg='#101720', width=220)
    sidebar.pack(side='left', fill='y')
    sidebar.pack_propagate(False)
    from PIL import Image, ImageTk
    import sys
    asset_root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    icon_path = asset_root / 'monitor_assets' / 'monitor.ico'
    if icon_path.exists():
        root.monitor_logo = ImageTk.PhotoImage(Image.open(icon_path).resize((26, 26)))
        root.monitor_window_icon = ImageTk.PhotoImage(Image.open(icon_path).convert('RGBA'))
        root.iconbitmap(default=str(icon_path))
        root.iconphoto(True, root.monitor_window_icon)
    tk.Label(sidebar, text='  后台库存监控', image=getattr(root, 'monitor_logo', ''), compound='left', bg='#101720', fg='white',
             font=('Microsoft YaHei UI', 14, 'bold'), anchor='w').pack(fill='x', padx=20, pady=(28, 4))
    tk.Label(sidebar, text='INVENTORY CONSOLE', bg='#101720', fg='#8491A5',
             font=('Segoe UI', 8), anchor='w').pack(fill='x', padx=22)
    tk.Label(sidebar, text='工作区', bg='#101720', fg='#718097', anchor='w').pack(fill='x', padx=22, pady=(32, 10))
    nav = tk.Frame(sidebar, bg='#101720')
    nav.pack(fill='x', padx=12)
    status_area = tk.Frame(sidebar, bg='#101720')
    status_area.pack(side='bottom', fill='x', padx=20, pady=20)
    tk.Frame(status_area, bg='#293345', height=1).pack(fill='x', pady=(0, 14))
    erp_status = tk.Label(status_area, text='●  等待 ERP 连接', bg='#101720', fg='#D99A42', anchor='w')
    erp_status.pack(fill='x')
    tk.Label(status_area, text='ERP 采集 · 机器人自动通知' if live else '接收 ERP 采集 · 不发送群消息', bg='#101720', fg='#8491A5',
             font=('Microsoft YaHei UI', 8), anchor='w').pack(fill='x', pady=(8, 0))
    workspace = tk.Frame(root, bg=BG)
    workspace.pack(side='left', fill='both', expand=True)
    topbar = tk.Frame(workspace, bg='white', padx=26, pady=14)
    topbar.pack(fill='x')
    tk.Label(topbar, text='后台库存监控   /   库存工作台', bg='white', fg=MUTED, anchor='w').pack(side='left')
    panel_version = tk.Label(topbar, text=APP_TITLE if live else '库存监控  ·  界面预览', bg='white', fg=INK)
    panel_version.pack(side='right')
    heading = tk.Frame(workspace, bg=BG, padx=22, pady=12)
    heading.pack(fill='x')
    page_title = tk.Label(heading, text='库存工作台', bg=BG, fg=INK, font=('Microsoft YaHei UI', 20, 'bold'), anchor='w')
    page_title.pack(anchor='w')
    page_description = tk.Label(heading, text='关注重点产品，查看库存变化与预警。', bg=BG, fg=MUTED, anchor='w')
    page_description.pack(anchor='w', pady=(4, 0))
    panel = InventoryPanel(workspace, service, on_hide=getattr(root, 'hide_to_tray', root.iconify))
    panel.live_delivery = live
    panel.version_label = panel_version
    panel.dingtalk_controller = dingtalk_controller
    panel.sidebar_erp_status = erp_status
    panel.refresh()
    panel.pack(fill='both', expand=True)
    buttons = []
    def navigate(index, action):
        for i, button in enumerate(buttons):
            button.config(bg='#202D44' if i == index else '#101720', fg='white' if i == index else '#A6B3C8')
        action()
    actions = [('▦  库存监控', lambda: panel.show_module('monitor')),
               ('＋  添加产品', panel.choose_catalog), ('⚙  采集设置', lambda: panel.show_module('settings')),
               ('▤  货源汇报', lambda: panel.show_module('daily')),
               ('↻  立即检查', lambda: (panel.show_module('monitor'), panel.check()))]
    if dingtalk_controller is not None:
        actions.insert(1, ('▤  钉钉监控', lambda: panel.show_module('dingtalk')))
    for index, (label, action) in enumerate(actions):
        button = tk.Button(nav, text=label, command=lambda i=index,a=action: navigate(i,a),
                           bg='#202D44' if index == 0 else '#101720', fg='white' if index == 0 else '#A6B3C8',
                           activebackground='#273650', activeforeground='white', relief='flat',
                           anchor='w', padx=14, pady=12, cursor='hand2')
        button.pack(fill='x', pady=3)
        buttons.append(button)
    def page_changed(module):
        titles = {'monitor': ('库存监控', '已关注的重点产品及库存变化。', 0),
                  'search': ('添加产品', '按 ERP 商品编号或名称搜索，选择后配置预警。', 1),
                  'config': ('产品配置', '核对商品信息，设置预警数量并保存。', 1),
                  'settings': ('采集设置', '连接公司大库的 ERP 库存采集。', 2)}
        titles['dingtalk'] = ('钉钉监控', '管理消息来源、连接状态和机器人转发。', 1)
        titles['daily'] = ('货源汇报', '一键生成货源周报，设置日报发送时段与 AI 整理。', 3)
        title, description, index = titles[module]
        if dingtalk_controller is not None and module in ('search', 'config', 'settings', 'daily'):
            index += 1
        page_title.config(text=title)
        page_description.config(text=description)
        for i, button in enumerate(buttons):
            button.config(bg='#202D44' if i == index else '#101720', fg='white' if i == index else '#A6B3C8')
    panel.on_module_changed = page_changed
    panel.show_module('monitor')
    return panel


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
