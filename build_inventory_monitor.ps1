#requires -Version 7.0
[CmdletBinding()]
param([string]$DwsPath = (Join-Path $env:USERPROFILE '.local/bin/dws.exe'),
      [string]$OutputDirectory = 'outputs/钉钉库存联动', [switch]$VerifyExisting, [switch]$Rebuild)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$packageDirectory = Join-Path $projectRoot $OutputDirectory
$executable = Join-Path $packageDirectory (((& py -3 -X utf8 -c "from inventory_version import EXE_NAME; print(EXE_NAME)").Trim()) + '.exe')
if ((Test-Path -LiteralPath $executable) -and -not $VerifyExisting -and -not $Rebuild) {
    throw "已存在库存联动版 EXE，拒绝覆盖：$executable。请先自行归档旧版本。"
}
$requiredAssets = @('douyin_notification.py', 'douyin_low_stock.py', 'douyin_low_stock_ui.py', 'douyin_browser.py', 'douyin_managed_export.py', 'inventory_browser_check.py', 'douyin_shops_ui.py', 'douyin_exclusions.py', 'douyin_exclusions_ui.py', 'notification_enterprise.py', 'notification_robot_editor.py', 'douyin_online.py', 'douyin_restock.py', 'douyin_restock_ui.py', 'douyin_maintenance.py', 'douyin_settings_ui.py', 'douyin_export_cleanup.py', 'inventory-setup.md', 'inventory-erp.user.js', 'monitor_assets/monitor.ico',
                    'packaging/inventory_monitor.spec',
                    'douyin_export.py', 'douyin_inventory.py', 'douyin_inventory_ui.py', 'douyin_inventory_runtime.py')
foreach ($relativePath in $requiredAssets) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath) -PathType Leaf)) {
        throw "缺少打包依赖：$relativePath"
    }
}
if (-not (Test-Path -LiteralPath $DwsPath -PathType Leaf)) { throw '缺少 DWS 可执行程序。请用 -DwsPath 指定。' }
$DwsPath = (Resolve-Path -LiteralPath $DwsPath).Path
& py -3 -c 'import PyInstaller, PIL, tkinter, websockets; print("PyInstaller", PyInstaller.__version__, "Pillow", PIL.__version__)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3 缺少 PyInstaller、Pillow、Tkinter 或 websockets（构建验证版本 15.0.1）。' }

$buildDirectory = Join-Path $projectRoot ('.build/inventory_build_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
New-Item -ItemType Directory -Path $buildDirectory, $packageDirectory -Force | Out-Null
if (-not $VerifyExisting) {
$previousDwsEnvironment = $env:INVENTORY_BUILD_DWS
try {
    $env:INVENTORY_BUILD_DWS = $DwsPath
    & py -3 -X utf8 -m PyInstaller --workpath $buildDirectory --distpath $packageDirectory (Join-Path $projectRoot 'packaging/inventory_monitor.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失败。原包未修改。' }
} finally {
    $env:INVENTORY_BUILD_DWS = $previousDwsEnvironment
}

# 抖店改用本机普通 Chrome 的官方导出，不再打包旧认证查询 worker。
}

if (-not $VerifyExisting) {
    $forbidden = @(Get-ChildItem -LiteralPath $packageDirectory -Recurse -Force | Where-Object {
        $_.Name -in @('.venv', 'browser_profile', 'profiles', 'cookies.json', 'data') -or
        ($_.PSIsContainer -eq $false -and $_.Extension -in @('.sqlite3', '.db'))
    })
    if ($forbidden.Count) { throw '候选包包含运行状态、浏览器资料或业务数据库。' }
}

# Read only the new executable's archive index; do not execute the desktop app.
$archiveCheck = @'
import json, sys
from PyInstaller.archive.readers import CArchiveReader
archive = CArchiveReader(sys.argv[1])
names = set(archive.toc)
required_assets = {'dws.exe', 'inventory-setup.md', 'inventory-erp.user.js', 'douyin_dom_bridge.mjs', 'douyin_erp_bridge.py'}
assert required_assets <= names, required_assets - names
pyz = archive.open_embedded_archive('PYZ.pyz')
modules = set(pyz.toc)
assert {'douyin_online', 'douyin_restock', 'douyin_restock_ui', 'douyin_export', 'douyin_inventory', 'douyin_inventory_ui', 'douyin_stock_page', 'douyin_erp', 'douyin_scope', 'douyin_maintenance', 'douyin_settings_ui', 'douyin_export_cleanup', 'pyxlsb'} <= modules
required_modules = {'douyin_notification', 'douyin_browser', 'douyin_managed_export', 'inventory_browser_check', 'websockets.sync.client', 'douyin_shops_ui', 'douyin_exclusions', 'douyin_exclusions_ui', 'notification_enterprise', 'notification_robot_editor', 'inventory_workspace', 'inventory_health', 'inventory_impact', 'inventory_insight_ui', 'inventory_message_view', 'inventory_refactor_check', 'inventory_sources', 'inventory_sql_service', 'sql_inventory_source', 'inventory_source_ui', 'inventory_event_ui', 'inventory_sql_ui_check', 'pymssql', 'inventory_remove_ui', 'inventory_import', 'inventory_import_ui', 'openpyxl', 'inventory_browser_recovery', 'inventory_connection_alert', 'notification_robots', 'notification_robot_ui', 'inventory_theme', 'inventory_ui_check', 'inventory_daily', 'inventory_weekly', 'inventory_weekly_ui', 'inventory_report_preview', 'inventory_report_delivery', 'inventory_cache', 'inventory_cache_ui', 'inventory_ai', 'inventory_monitor', 'inventory_rules', 'inventory_warehouses', 'inventory_panel', 'inventory_bridge', 'profit_monitor', 'erp_price_monitor', 'profit_manifest', 'profit_rules', 'profit_panel', 'douyin_inventory_runtime',
                    'inventory_delivery', 'inventory_inbound', 'monitor_setup_service', 'monitor_portable_install',
                    'source_monitor_supervisor', 'pystray._win32', 'tkinter'}
assert required_modules <= modules, required_modules - modules
for name in names:
    assert not name.endswith(('.sqlite3', '.db')), name
    assert name.rsplit('/', 1)[-1] not in {'monitor_sources.json', 'monitor_settings.json', 'control.json'}, name
print(json.dumps({'assets': sorted(required_assets), 'modules': sorted(required_modules)}, ensure_ascii=False))
'@
$archiveEvidence = & py -3 -c $archiveCheck $executable
if ($LASTEXITCODE -ne 0) { throw '打包归档依赖验证失败，请勿交付。' }
foreach ($asset in @('inventory-setup.md', 'inventory-erp.user.js')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $asset) -Destination (Join-Path $packageDirectory $asset)
}
$manifest = [ordered]@{
    created_at = (Get-Date -Format o)
    desktop_executed = $false
    runtime_data_included = $false
    archive_verification = ($archiveEvidence | ConvertFrom-Json)
    # 新视图和领域模块一并记录源码哈希，便于核对实际交付内容。
    source_files = @(@('douyin_notification.py', 'douyin_low_stock.py', 'douyin_low_stock_ui.py', 'douyin_browser.py', 'douyin_managed_export.py', 'inventory_browser_check.py', 'douyin_shops_ui.py', 'douyin_exclusions.py', 'douyin_exclusions_ui.py', 'notification_enterprise.py', 'notification_robot_editor.py', 'douyin_online.py', 'douyin_restock.py', 'douyin_restock_ui.py', 'douyin_maintenance.py', 'douyin_settings_ui.py', 'douyin_export_cleanup.py', 'douyin_scope.py', 'douyin_stock_page.py', 'douyin_dom_bridge.mjs', 'douyin_erp.py', 'douyin_erp_bridge.py', 'douyin_export.py', 'douyin_inventory.py', 'douyin_inventory_ui.py', 'inventory_workspace.py', 'inventory_health.py', 'inventory_impact.py', 'inventory_insight_ui.py', 'inventory_message_view.py', 'inventory_refactor_check.py', 'inventory_sources.py', 'inventory_sql_service.py', 'sql_inventory_source.py', 'inventory_source_ui.py', 'inventory_event_ui.py', 'inventory_sql_ui_check.py', 'inventory_remove_ui.py', 'inventory_import.py', 'inventory_import_ui.py', 'inventory_version.py', 'notification_robots.py', 'notification_robot_ui.py', 'inventory_theme.py', 'inventory_ui_check.py', 'inventory_daily.py', 'inventory_weekly.py', 'inventory_weekly_ui.py', 'inventory_report_preview.py', 'inventory_report_delivery.py', 'inventory_cache.py', 'inventory_cache_ui.py', 'inventory_recycle.py', 'inventory_ai.py', 'dingtalk_panel.py', 'inventory_app.py', 'test_inventory_ui_local.py', 'monitor_tray_app.py', 'inventory_panel.py', 'inventory_monitor.py', 'douyin_inventory_runtime.py', 'douyin_inventory_worker.py',
                      'inventory_rules.py', 'inventory_warehouses.py', 'inventory_bridge.py', 'inventory_delivery.py',
                      'inventory_inbound.py', 'source_monitor_supervisor.py', 'suhao_cost_monitor.py',
                      'profit_monitor.py', 'erp_price_monitor.py', 'profit_manifest.py', 'profit_rules.py', 'profit_panel.py',
                      'monitor_app_control.py', 'monitor_setup_service.py', 'monitor_setup_wizard.py',
                      'monitor_portable_install.py', 'inventory-setup.md', 'inventory-erp.user.js',
                      'packaging/inventory_monitor.spec', 'build_inventory_monitor.ps1') | ForEach-Object {
        [ordered]@{name = $_; sha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot $_) -Algorithm SHA256).Hash}
    })
    files = @(@($executable,
                (Join-Path $packageDirectory 'inventory-setup.md'),
                (Join-Path $packageDirectory 'inventory-erp.user.js')) | ForEach-Object {
        $_ = Get-Item -LiteralPath $_
        [ordered]@{name = [IO.Path]::GetRelativePath($packageDirectory, $_.FullName); size = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    })
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $packageDirectory 'build-manifest.json') -Encoding utf8
Write-Host "独立打包及归档验证通过：$packageDirectory"
