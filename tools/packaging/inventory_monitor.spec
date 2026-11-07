# -*- mode: python ; coding: utf-8 -*-
"""Isolated inventory build: explicit public assets, no runtime data directories."""
from pathlib import Path
import os
import runpy

root = Path(SPECPATH).parents[1]
source = root / 'app' / 'src'
ui = root / 'app' / 'ui'
dependencies = Path(os.environ.get('INVENTORY_BUILD_DEPENDENCIES', str(root)))
a = Analysis(
    [str(source / 'inventory_app.py')],
    pathex=[str(source), str(dependencies / '.codex_tmp' / 'tray_deps'), str(dependencies / 'beta' / 'sql_inventory_20260917' / 'deps')],
    binaries=[(os.environ['INVENTORY_BUILD_DWS'], '.')],
    datas=[(str(root / 'docs' / 'inventory-setup.md'), '.'),
           (str(source / 'douyin_dom_bridge.mjs'), '.'),
           (str(source / 'douyin_erp_bridge.py'), '.'),
           (str(ui / 'inventory-erp.user.js'), '.'),
           # 只打包已构建的网页与更新公钥，不包含开发依赖或签名私钥。
           (str(ui / 'console'), 'console'),
           (str(ui / 'console-host'), 'console-host'),
           (str(ui / 'update-public-key.txt'), '.'),
           (str(ui / 'apply-update.ps1'), '.'),
           (str(root / 'THIRD_PARTY_LICENSES' / 'WebView2-LICENSE.txt'), 'licenses'),
           (str(ui / 'monitor_assets' / 'monitor.ico'), 'monitor_assets')],
    hiddenimports=['pystray._win32', 'inventory_monitor', 'inventory_rules',
                   'inventory_business', 'inventory_business_actions', 'inventory_business_views', 'inventory_console', 'order_bot_adapter', 'inventory_updater',
                   'profit_monitor', 'erp_price_monitor', 'profit_manifest', 'profit_rules', 'profit_panel',
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
