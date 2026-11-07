"""正式工作台界面装配；测试入口复用此模块。"""
from pathlib import Path
import tkinter as tk
from inventory_panel import InventoryPanel
from inventory_version import APP_TITLE


def build_workspace(root, service, live=False, dingtalk_controller=None):
    from tkinter import ttk
    from inventory_panel import BG, INK, MUTED, BLUE
    from inventory_theme import install_theme
    install_theme(root)
    sidebar = tk.Frame(root, bg='#101720', width=200)
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
             font=('Microsoft YaHei UI', 14, 'bold'), anchor='w').pack(fill='x', padx=20, pady=(22, 4))
    tk.Label(sidebar, text='INVENTORY CONSOLE', bg='#101720', fg='#8491A5',
             font=('Segoe UI', 8), anchor='w').pack(fill='x', padx=22)
    tk.Label(sidebar, text='工作区', bg='#101720', fg='#718097', anchor='w').pack(fill='x', padx=22, pady=(20, 8))
    nav = tk.Frame(sidebar, bg='#101720')
    nav.pack(fill='x', padx=12)
    status_area = tk.Frame(sidebar, bg='#101720')
    status_area.pack(side='bottom', fill='x', padx=20, pady=12)
    status_divider = tk.Frame(status_area, bg='#293345', height=1)
    status_divider.pack(fill='x', pady=(0, 14))
    erp_status = tk.Label(status_area, text='●  等待 ERP 连接', bg='#101720', fg='#D99A42', anchor='w')
    erp_status.pack(fill='x')
    tk.Label(status_area, text='ERP 采集 · 机器人自动通知' if live else '接收 ERP 采集 · 不发送群消息', bg='#101720', fg='#8491A5',
             font=('Microsoft YaHei UI', 8), anchor='w').pack(fill='x', pady=(8, 0))
    workspace = tk.Frame(root, bg=BG)
    workspace.pack(side='left', fill='both', expand=True)
    topbar = tk.Frame(workspace, bg='white', padx=26, pady=14)
    # 页面标题已说明当前位置，不再为重复面包屑占用一整行。
    tk.Label(topbar, text='后台库存监控   /   库存工作台', bg='white', fg=MUTED, anchor='w').pack(side='left')
    panel_version = tk.Label(topbar, text=APP_TITLE if live else '库存监控  ·  界面预览', bg='white', fg=INK)
    panel_version.pack(side='right')
    heading = tk.Frame(workspace, bg=BG, padx=22, pady=8)
    heading.pack(fill='x')
    page_title = tk.Label(heading, text='库存工作台', bg=BG, fg=INK, font=('Microsoft YaHei UI', 18, 'bold'), anchor='w')
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
    # 功能键绑定页面名称，新增左侧入口不再依赖容易错位的数字下标。
    actions = [('monitor', '▦  库存监控', lambda: panel.show_module('monitor')),
               ('daily', '▤  货源汇报', lambda: panel.show_module('daily')),
               ('profit', '▤  ERP 价格监控', lambda: panel.show_module('profit'))]
    if dingtalk_controller is not None:
        actions.insert(1, ('dingtalk', '▤  钉钉监控', lambda: panel.show_module('dingtalk')))
    if hasattr(service,'preview_events'):
        actions.append(('events', '▤  库存提醒记录', lambda: panel.show_module('events')))
    actions.append(('douyin', '▦  抖店库存', lambda: panel.show_module('douyin')))
    actions.append(('impact', '▤  受影响配置', lambda: panel.show_module('impact')))
    actions.append(('check', '↻  立即检查', lambda: (panel.show_module('monitor'), panel.check())))
    actions.append(('settings', '⚙  设置', lambda: panel.show_module('settings')))
    if live:
        panel.ensure_douyin_runtime()
    panel.navigation_buttons = {}
    for index, (key, label, action) in enumerate(actions):
        # 全局立即检查固定在左栏底部、ERP 状态上方，不随功能入口数量改变位置。
        button = tk.Button(status_area if key in ('check', 'settings') else nav, text=label, command=lambda i=index,a=action: navigate(i,a),
                           bg='#202D44' if index == 0 else '#101720', fg='white' if index == 0 else '#A6B3C8',
                           activebackground='#273650', activeforeground='white', relief='flat',
                           anchor='w', padx=12, pady=4, cursor='hand2')
        if key in ('check', 'settings'):
            button.pack(fill='x', pady=(0, 10), before=status_divider)
        else:
            button.pack(fill='x', pady=1)
        buttons.append(button)
        panel.navigation_buttons[key] = button
    def page_changed(module):
        titles = {'monitor': ('库存监控', '已关注的重点产品及库存变化；立即检查同时刷新 ERP 和抖店库存。'),
                  'search': ('添加产品', '按 ERP 商品编号或名称搜索，选择后配置预警。'),
                  'config': ('产品配置', '核对商品信息，设置预警数量并保存。'),
                  'settings': ('设置', '按功能管理采集、巡检、价格和通知设置。'),
                  'robots': ('管理机器人', '统一管理企业机器人、Webhook、通知类型与启停状态。')}
        titles['dingtalk'] = ('钉钉监控', '管理消息来源、连接状态和机器人转发。')
        titles['daily'] = ('货源汇报', '一键生成货源周报，设置日报发送时段与 AI 整理。')
        titles['profit'] = ('ERP 价格监控', '按配置关联的配件，查看公司大库单价变化。')
        titles['events'] = ('库存提醒记录', '筛选变化，选中后查看详情；查看和导出不重新发送。')
        titles['douyin'] = ('抖店库存', '官方导出库存＋主机维护表配件＋ERP 实时库存；补库存提醒按已启用机器人发送。')
        titles['impact'] = ('受影响配置', '从低库存配件定位需要核对的店铺、SPU 和配置。')
        title, description = titles['settings'] if panel.settings_context else titles[module]
        page_title.config(text=title)
        page_description.config(text=description)
        for key, button in panel.navigation_buttons.items():
            # 添加与编辑共用库存入口；设置子页始终高亮底部设置。
            selected = key == ('settings' if panel.settings_context else 'monitor' if module in ('search', 'config') else module)
            button.config(bg='#202D44' if selected else '#101720', fg='white' if selected else '#A6B3C8')
    panel.on_module_changed = page_changed
    panel.show_module('monitor')
    return panel
