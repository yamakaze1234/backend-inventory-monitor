# -*- mode: python ; coding: utf-8 -*-
"""Isolated inventory build: explicit public assets, no runtime data directories."""
from pathlib import Path
import os
import runpy

root = Path(SPECPATH).parents[1]
source = root / 'app' / 'src'
ui = root / 'app' / 'ui'
# 仅收录兼容后端的公开源码，保持目录结构，排除编译缓存与运行数据。
order_root = root / 'integrations' / 'order_backend'
order_sources = [(str(p), str(Path('order_backend') / p.parent.relative_to(order_root))) for p in sorted(order_root.rglob('*')) if p.is_file() and p.suffix in {'.py', '.pyw', '.ps1', '.mjs'}]
# 运行依赖统一由uv.lock 安装到项目环境，不借用作者电脑的缓存。
a = Analysis(
    [str(source / 'inventory_app.py')],
    pathex=[str(source)],
    binaries=[(os.environ['INVENTORY_BUILD_DWS'], '.')],
    datas=[(str(root / 'docs' / 'inventory-setup.md'), '.'),
           # CWS 在独立 Python 中执行公开脚本，运行配置仍留在工作区。
           (str(root / 'integrations/cws/DY-官方库存巡检.py'), 'cws'),
           *[(str(source / (name + '.py')), 'cws-support') for name in
             ('douyin_cws_export', 'douyin_export', 'douyin_managed_export',
              'douyin_browser', 'douyin_inventory', 'douyin_scope')],
           (str(source / 'douyin_dom_bridge.mjs'), '.'),
           (str(source / 'douyin_erp_bridge.py'), '.'),
           (str(ui / 'inventory-erp.user.js'), '.'),
           # 只打包已构建的网页与更新公钥，不包含开发依赖或签名私钥。
           (str(ui / 'console'), 'console'),
           (str(ui / 'console-host'), 'console-host'),
           (str(ui / 'update-public-key.txt'), '.'),
           (str(ui / 'apply-update.ps1'), '.'),
           (str(root / 'THIRD_PARTY_LICENSES' / 'WebView2-LICENSE.txt'), 'licenses'),
           (str(ui / 'monitor_assets' / 'monitor.ico'), 'monitor_assets')] + order_sources,
    hiddenimports=['pystray._win32', 'inventory_monitor', 'inventory_rules',
                   'notification_resolver', 'notification_business', 'order_business', 'order_bot_config', 'inventory_workspace_config', 'inventory_cache_schedule', 'inventory_price_changes', 'inventory_data_api', 'inventory_business', 'inventory_business_actions', 'inventory_business_views', 'inventory_schedule_view', 'inventory_console', 'order_bot_adapter', 'inventory_updater',
                   'price_maintenance', 'price_schedule', 'profit_monitor', 'erp_price_monitor', 'profit_manifest', 'profit_rules', 'profit_panel',
                   'inventory_panel', 'inventory_bridge', 'inventory_delivery', 'inventory_inbound', 'source_monitor_supervisor', 'monitor_portable_install', 'monitor_setup_service'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['numpy', 'pandas', 'scipy', 'matplotlib'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
    name=runpy.run_path(str(source / 'inventory_version.py'))['EXE_NAME'], debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False,
    icon=[str(ui / 'monitor_assets' / 'monitor.ico')])
