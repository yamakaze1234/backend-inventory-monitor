#requires -Version 7.0
[CmdletBinding()]
param([string]$DwsPath = (Join-Path $env:USERPROFILE '.local/bin/dws.exe'),
      [string]$OutputDirectory = 'dist/backend-inventory-monitor-v1.03', [switch]$VerifyExisting, [switch]$Rebuild)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$packageDirectory = Join-Path $projectRoot $OutputDirectory
$executable = Join-Path $packageDirectory (((& py -3 -X utf8 -c "from inventory_version import EXE_NAME; print(EXE_NAME)").Trim()) + '.exe')
if ((Test-Path -LiteralPath $executable) -and -not $VerifyExisting -and -not $Rebuild) {
    throw "已存在库存联动版 EXE，拒绝覆盖：$executable。请先自行归档旧版本。"
}
$requiredAssets = @('inventory-setup.md', 'inventory-erp.user.js', 'monitor_assets/monitor.ico',
                    'packaging/inventory_monitor.spec')
foreach ($relativePath in $requiredAssets) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath) -PathType Leaf)) {
        throw "缺少打包依赖：$relativePath"
    }
}
if (-not (Test-Path -LiteralPath $DwsPath -PathType Leaf)) { throw '缺少 DWS 可执行程序。请用 -DwsPath 指定。' }
$DwsPath = (Resolve-Path -LiteralPath $DwsPath).Path
& py -3 -c 'import PyInstaller, PIL, tkinter, pystray, pymssql; print("PyInstaller", PyInstaller.__version__, "Pillow", PIL.__version__)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3 缺少 PyInstaller、Pillow、pystray 或 Tkinter。' }

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

}

# Read only the new executable's archive index; do not execute the desktop app.
$archiveCheck = @'
import json, sys
from PyInstaller.archive.readers import CArchiveReader
archive = CArchiveReader(sys.argv[1])
names = set(archive.toc)
required_assets = {'dws.exe', 'inventory-setup.md', 'inventory-erp.user.js'}
assert required_assets <= names, required_assets - names
pyz = archive.open_embedded_archive('PYZ.pyz')
modules = set(pyz.toc)
required_modules = {'pymssql', 'sql_inventory_source', 'inventory_sources', 'inventory_sql_service', 'inventory_source_ui', 'inventory_event_ui', 'inventory_sql_ui_check', 'inventory_import', 'inventory_import_ui', 'inventory_remove_ui', 'openpyxl', 'inventory_browser_recovery', 'inventory_connection_alert', 'notification_robots', 'notification_robot_ui', 'inventory_theme', 'inventory_ui_check', 'inventory_daily', 'inventory_weekly', 'inventory_weekly_ui', 'inventory_report_preview', 'inventory_report_delivery', 'inventory_cache', 'inventory_cache_ui', 'inventory_recycle', 'inventory_ai', 'inventory_monitor', 'inventory_rules', 'inventory_warehouses', 'inventory_panel', 'inventory_bridge',
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
foreach ($asset in @('inventory-setup.md', 'inventory-erp.user.js', '使用说明.md', 'prepare-environment.ps1', '一键准备运行环境.cmd')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $asset) -Destination (Join-Path $packageDirectory $asset)
}
Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_LICENSES') -Destination $packageDirectory -Recurse -Force
& py -3 -X utf8 (Join-Path $projectRoot 'build_user_guide.py') (Join-Path $packageDirectory '完整使用教程.html')
if ($LASTEXITCODE -ne 0) { throw 'HTML 教程生成失败。' }
$manifest = [ordered]@{
    created_at = (Get-Date -Format o)
    desktop_executed = $false
    runtime_data_included = $false
    archive_verification = ($archiveEvidence | ConvertFrom-Json)
    source_files = @(@('sql_inventory_source.py', 'inventory_sources.py', 'inventory_sql_service.py', 'inventory_source_ui.py', 'inventory_event_ui.py', 'inventory_sql_ui_check.py', 'inventory_import.py', 'inventory_import_ui.py', 'inventory_remove_ui.py', 'inventory_version.py', 'notification_robots.py', 'notification_robot_ui.py', 'inventory_theme.py', 'inventory_ui_check.py', 'inventory_daily.py', 'inventory_weekly.py', 'inventory_weekly_ui.py', 'inventory_report_preview.py', 'inventory_report_delivery.py', 'inventory_cache.py', 'inventory_cache_ui.py', 'inventory_recycle.py', 'inventory_ai.py', 'dingtalk_panel.py', 'inventory_app.py', 'test_inventory_ui_local.py', 'monitor_tray_app.py', 'inventory_panel.py', 'inventory_monitor.py',
                      'inventory_browser_recovery.py', 'inventory_connection_alert.py', 'build_user_guide.py', 'prepare-environment.ps1', '一键准备运行环境.cmd', 'inventory_rules.py', 'inventory_warehouses.py', 'inventory_bridge.py', 'inventory_delivery.py',
                      'inventory_inbound.py', 'source_monitor_supervisor.py', 'suhao_cost_monitor.py',
                      'monitor_app_control.py', 'monitor_setup_service.py', 'monitor_setup_wizard.py',
                      'monitor_portable_install.py', 'inventory-setup.md', 'inventory-erp.user.js',
                      'packaging/inventory_monitor.spec') | ForEach-Object {
        [ordered]@{name = $_; sha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot $_) -Algorithm SHA256).Hash}
    })
    files = @(Get-ChildItem -LiteralPath $packageDirectory -File | ForEach-Object {
        [ordered]@{name = $_.Name; size = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    })
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $packageDirectory 'build-manifest.json') -Encoding utf8
Write-Host "独立打包及归档验证通过：$packageDirectory"
