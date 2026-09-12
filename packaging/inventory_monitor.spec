# -*- mode: python ; coding: utf-8 -*-
"""Isolated inventory build: explicit public assets, no runtime data directories."""
from pathlib import Path
import os
import runpy

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / 'inventory_app.py')],
    pathex=[str(root)],
    binaries=[(os.environ['INVENTORY_BUILD_DWS'], '.')],
    datas=[(str(root / 'inventory-setup.md'), '.'),
           (str(root / 'inventory-erp.user.js'), '.'),
           (str(root / 'monitor_assets' / 'monitor.ico'), 'monitor_assets')],
    hiddenimports=['pystray._win32', 'inventory_monitor', 'inventory_rules',
                   'inventory_panel', 'inventory_bridge', 'inventory_delivery', 'inventory_inbound', 'source_monitor_supervisor', 'monitor_portable_install', 'monitor_setup_service'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['numpy', 'pandas', 'scipy', 'matplotlib'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
    name=runpy.run_path(str(root / 'inventory_version.py'))['EXE_NAME'], debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False,
    icon=[str(root / 'monitor_assets' / 'monitor.ico')])
