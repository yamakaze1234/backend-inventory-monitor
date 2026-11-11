#requires -Version 7.0
[CmdletBinding()]
param([string]$DwsPath = (Join-Path $env:USERPROFILE '.local/bin/dws.exe'),
      [string]$OutputDirectory = 'outputs/钉钉库存联动', [switch]$VerifyExisting, [switch]$Rebuild, [string]$PythonExecutable,
      [string]$DependencyRoot, [string]$IndependentRoot)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$sourceRoot = Join-Path $projectRoot 'app/src'
if (-not $PythonExecutable) {
    # 使用项目虚拟环境或 PATH，避免要求固定盘符。
    $localPython=Join-Path $projectRoot '.venv/Scripts/python.exe'
    $PythonExecutable=if(Test-Path -LiteralPath $localPython){$localPython}else{'python'}
}
# 旧调用参数保留兼容；依赖必须安装到所选解释器，不再加载个人缓存。
if($DependencyRoot){Write-Warning 'DependencyRoot 已不再注入缓存，请将 requirements 安装到 PythonExecutable。'}
if (-not $IndependentRoot) { $IndependentRoot = Join-Path (Split-Path $projectRoot -Parent) '独立工具' }
# 构建使用明确的 Python 与源码路径，避免依赖当前工作目录。
$env:PYTHONPATH = $sourceRoot
# 构建始终核验当前前端，避免候选 EXE 夹带过期网页。
if (-not $VerifyExisting) {
    & (Join-Path $projectRoot 'tools/build_console_host.ps1')
    & npm --prefix (Join-Path $projectRoot 'app/web') run build
    if ($LASTEXITCODE -ne 0) { throw 'ReUI 工作台构建失败' }
}
function py { $pythonArgs = @($args | Where-Object { $_ -ne '-3' }); & $PythonExecutable @pythonArgs }
function Resolve-ProjectSource([string]$name) {
    # 旧清单中的独立组件从集中目录解析，不再占用根目录。
    if ($name.StartsWith('抖店库存查询工具/')) { return Join-Path $IndependentRoot $name }
    if ($name -eq '.codex_tmp/inventory_monitor.spec') { return Join-Path $projectRoot 'tools/packaging/inventory_monitor.spec' }
    if ($name -eq 'build_inventory_monitor.ps1') { return Join-Path $projectRoot 'tools/build_inventory_monitor.ps1' }
    if ($name.StartsWith('monitor_assets/') -or $name -eq 'inventory-erp.user.js') { return Join-Path (Join-Path $projectRoot 'app/ui') $name }
    if ($name -eq 'inventory-setup.md') { return Join-Path (Join-Path $projectRoot 'docs') $name }
    if (Test-Path -LiteralPath (Join-Path $sourceRoot $name)) { return Join-Path $sourceRoot $name }
    return Join-Path $projectRoot $name
}
$packageDirectory = Join-Path $projectRoot $OutputDirectory
$executable = Join-Path $packageDirectory (((& py -3 -X utf8 -c "from inventory_version import EXE_NAME; print(EXE_NAME)").Trim()) + '.exe')
if ((Test-Path -LiteralPath $executable) -and -not $VerifyExisting -and -not $Rebuild) {
    throw "已存在库存联动版 EXE，拒绝覆盖：$executable。请先自行归档旧版本。"
}
$requiredAssets = @('project_paths.py', 'douyin_online.py', 'douyin_notification.py', 'douyin_browser.py', 'douyin_managed_export.py', 'inventory_browser_check.py', 'douyin_shops_ui.py', 'douyin_exclusions.py', 'douyin_exclusions_ui.py', 'notification_enterprise.py', 'notification_robot_editor.py', 'douyin_online.py', 'douyin_restock.py', 'douyin_restock_ui.py', 'douyin_maintenance.py', 'douyin_settings_ui.py', 'douyin_export_cleanup.py', 'inventory-setup.md', 'inventory-erp.user.js', 'monitor_assets/monitor.ico',
                    '.codex_tmp/inventory_monitor.spec',
                    'douyin_export.py', 'douyin_inventory.py', 'douyin_inventory_ui.py', 'douyin_inventory_runtime.py')
foreach ($relativePath in $requiredAssets) {
    if (-not (Test-Path -LiteralPath (Resolve-ProjectSource $relativePath) -PathType Leaf)) {
        throw "缺少打包依赖：$relativePath"
    }
}
if (-not (Test-Path -LiteralPath $DwsPath -PathType Leaf)) { throw '缺少 DWS 可执行程序。请用 -DwsPath 指定。' }
$DwsPath = (Resolve-Path -LiteralPath $DwsPath).Path
& py -3 -c 'import PyInstaller, PIL, tkinter, websockets, pystray, cryptography; print("PyInstaller", PyInstaller.__version__, "Pillow", PIL.__version__)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3 缺少 PyInstaller、Pillow、Tkinter 或 websockets（构建验证版本 15.0.1）。' }

$buildDirectory = Join-Path $projectRoot ('.build/inventory_build_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
New-Item -ItemType Directory -Path $buildDirectory, $packageDirectory -Force | Out-Null
if (-not $VerifyExisting) {
$previousDwsEnvironment = $env:INVENTORY_BUILD_DWS
try {
    $env:INVENTORY_BUILD_DWS = $DwsPath
    & py -3 -X utf8 -m PyInstaller --workpath $buildDirectory --distpath $packageDirectory (Resolve-ProjectSource '.codex_tmp/inventory_monitor.spec')
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
names = {name.replace(chr(92), '/') for name in archive.toc}
# 部门兼容安装需要完整的 Python/Node/PowerShell 启动链。
required_assets = {'order_backend/matching/group_order_bot.py', 'order_backend/matching/robot_config.py', 'order_backend/matching/order_department_runtime.py', 'order_backend/control/order_department_runtime.py', 'order_backend/control/robot_settings.py', 'order_backend/control/robot_desktop_control.py', 'order_backend/control/robot-keepalive.pyw', 'order_backend/control/order-match-server.mjs', 'order_backend/control/启动机器人.ps1', 'dws.exe', 'inventory-setup.md', 'inventory-erp.user.js', 'douyin_dom_bridge.mjs', 'douyin_erp_bridge.py', 'console/index.html', 'console-host/InventoryConsoleHost.exe', 'update-public-key.txt', 'apply-update.ps1'}
assert required_assets <= names, required_assets - names
pyz = archive.open_embedded_archive('PYZ.pyz')
modules = set(pyz.toc)
assert {'douyin_online', 'douyin_restock', 'douyin_restock_ui', 'douyin_export', 'douyin_inventory', 'douyin_inventory_ui', 'douyin_stock_page', 'douyin_erp', 'douyin_scope', 'douyin_maintenance', 'douyin_settings_ui', 'douyin_export_cleanup', 'pyxlsb'} <= modules
required_modules = {'notification_resolver', 'notification_business', 'order_business', 'order_bot_config', 'inventory_workspace_config', 'inventory_cache_schedule', 'inventory_price_changes', 'inventory_shop_names', 'inventory_data_api', 'inventory_business', 'inventory_business_actions', 'inventory_business_views', 'inventory_console', 'inventory_updater', 'order_bot_adapter', 'cryptography.hazmat.primitives.asymmetric.ed25519', 'douyin_browser', 'douyin_managed_export', 'inventory_browser_check', 'websockets.sync.client', 'douyin_shops_ui', 'douyin_exclusions', 'douyin_exclusions_ui', 'notification_enterprise', 'notification_robot_editor', 'inventory_workspace', 'inventory_health', 'inventory_insight_ui', 'inventory_message_view', 'inventory_refactor_check', 'inventory_sources', 'inventory_sql_service', 'sql_inventory_source', 'inventory_source_ui', 'inventory_event_ui', 'inventory_sql_ui_check', 'pymssql', 'inventory_remove_ui', 'inventory_import', 'inventory_import_ui', 'openpyxl', 'inventory_browser_recovery', 'inventory_connection_alert', 'notification_robots', 'notification_robot_ui', 'inventory_theme', 'inventory_ui_check', 'inventory_daily', 'inventory_weekly', 'inventory_weekly_ui', 'inventory_report_preview', 'inventory_report_delivery', 'inventory_cache', 'inventory_cache_ui', 'inventory_ai', 'inventory_monitor', 'inventory_rules', 'inventory_warehouses', 'inventory_panel', 'inventory_bridge', 'profit_monitor', 'erp_price_monitor', 'profit_manifest', 'profit_rules', 'profit_panel', 'douyin_inventory_runtime',
                    'inventory_delivery', 'inventory_inbound', 'monitor_setup_service', 'monitor_portable_install',
                    'source_monitor_supervisor', 'pystray._win32', 'tkinter'}
assert required_modules <= modules, required_modules - modules
assert 'inventory_impact' not in modules, '已移除的受影响配置不应进入包'
for name in names:
    assert not name.endswith(('.sqlite3', '.db')), name
    assert name.rsplit('/', 1)[-1] not in {'monitor_sources.json', 'monitor_settings.json', 'control.json'}, name
print(json.dumps({'assets': sorted(required_assets), 'modules': sorted(required_modules)}, ensure_ascii=False))
'@
$archiveEvidence = & py -3 -c $archiveCheck $executable
if ($LASTEXITCODE -ne 0) { throw '打包归档依赖验证失败，请勿交付。' }
foreach ($asset in @('inventory-setup.md', 'inventory-erp.user.js')) {
    Copy-Item -LiteralPath (Resolve-ProjectSource $asset) -Destination (Join-Path $packageDirectory $asset)
}
# 查询客户端与接口契约随候选交付，不包含连接令牌。
Copy-Item -LiteralPath (Join-Path $projectRoot 'tools/query_inventory_data.py') -Destination $packageDirectory
# 普通用户获得可直接运行的部署入口与当前说明，不携带开发环境或个人配置。
$deliveryFiles=@('README.md','docs/使用说明.md','docs/结构与数据流.md','docs/开发与构建.md',
                 'docs/PUBLICATION.md','docs/inventory-setup.md','docs/机器人数据查询接口.md',
                 'integrations/order_backend/README.md')
foreach($relative in $deliveryFiles){
    # 保留文档的相对目录，发行包内的说明和结构链接也可离线查阅。
    $destination=Join-Path $packageDirectory $relative
    New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot $relative) -Destination $destination
}
Copy-Item -LiteralPath (Join-Path $projectRoot 'tools/一键部署环境.cmd') -Destination $packageDirectory
$licenseDirectory=Join-Path $packageDirectory 'THIRD_PARTY_LICENSES'
New-Item -ItemType Directory -Path $licenseDirectory -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_LICENSES') -File -Filter '*.txt' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $licenseDirectory
}
$packageTools=Join-Path $packageDirectory 'tools'
New-Item -ItemType Directory -Path $packageTools -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'tools/prepare-environment.ps1') -Destination $packageTools
# CMD 先切换 UTF-8，再按自身目录定位版本 EXE，支持中文及含空格的安装路径。
$launchLines=@('@echo off','chcp 65001 >nul',('start "" "%~dp0'+[IO.Path]::GetFileName($executable)+'"'))
[IO.File]::WriteAllText((Join-Path $packageDirectory '启动.cmd'),(($launchLines -join "`r`n")+"`r`n"),[Text.UTF8Encoding]::new($false))
$manifest = [ordered]@{
    created_at = (Get-Date -Format o)
    # 发行提交与工作树状态明确区分，候选不得伪称来自干净的发行提交。
    source_commit = (& git -C $projectRoot rev-parse HEAD)
    source_dirty = [bool](& git -C $projectRoot status --porcelain --untracked-files=no)
    version = ((& py -3 -c 'from inventory_version import VERSION; print(VERSION)').Trim())
    desktop_executed = $false
    runtime_data_included = $false
    archive_verification = ($archiveEvidence | ConvertFrom-Json)
    # 新视图和领域模块一并记录源码哈希，便于核对实际交付内容。
    source_files = @(@('monitor_assets/monitor.ico', 'InventoryConsoleHost.cs', 'tools/build_console_host.ps1', 'tools/query_inventory_data.py', 'docs/机器人数据查询接口.md', 'tools/packaging/requirements-update.txt', 'inventory_price_changes.py', 'inventory_shop_names.py', 'inventory_data_api.py', 'inventory_business.py', 'inventory_business_actions.py', 'inventory_business_views.py', 'inventory_console.py', 'inventory_updater.py', 'order_bot_adapter.py', 'app/ui/update-public-key.txt', 'app/ui/apply-update.ps1', 'project_paths.py', 'douyin_online.py', 'douyin_notification.py', 'douyin_browser.py', 'douyin_managed_export.py', 'inventory_browser_check.py', 'douyin_shops_ui.py', 'douyin_exclusions.py', 'douyin_exclusions_ui.py', 'notification_enterprise.py', 'notification_robot_editor.py', 'douyin_online.py', 'douyin_restock.py', 'douyin_restock_ui.py', 'douyin_maintenance.py', 'douyin_settings_ui.py', 'douyin_export_cleanup.py', 'douyin_scope.py', 'douyin_stock_page.py', 'douyin_dom_bridge.mjs', 'douyin_erp.py', 'douyin_erp_bridge.py', 'douyin_export.py', 'douyin_inventory.py', 'douyin_inventory_ui.py', 'inventory_workspace.py', 'inventory_health.py', 'inventory_insight_ui.py', 'inventory_message_view.py', 'inventory_refactor_check.py', 'inventory_sources.py', 'inventory_sql_service.py', 'sql_inventory_source.py', 'inventory_source_ui.py', 'inventory_event_ui.py', 'inventory_sql_ui_check.py', 'inventory_remove_ui.py', 'inventory_import.py', 'inventory_import_ui.py', 'inventory_version.py', 'notification_robots.py', 'notification_robot_ui.py', 'inventory_theme.py', 'inventory_ui_check.py', 'inventory_daily.py', 'inventory_weekly.py', 'inventory_weekly_ui.py', 'inventory_report_preview.py', 'inventory_report_delivery.py', 'inventory_cache.py', 'inventory_cache_ui.py', 'inventory_recycle.py', 'inventory_ai.py', 'dingtalk_panel.py', 'inventory_app.py', 'test_inventory_ui_local.py', 'monitor_tray_app.py', 'inventory_panel.py', 'inventory_monitor.py', 'douyin_inventory_runtime.py', 'douyin_inventory_worker.py',
                      'inventory_rules.py', 'inventory_warehouses.py', 'inventory_bridge.py', 'inventory_delivery.py',
                      'inventory_inbound.py', 'source_monitor_supervisor.py', 'suhao_cost_monitor.py',
                      'notification_resolver.py', 'notification_business.py', 'order_business.py', 'order_bot_config.py', 'inventory_workspace_config.py', 'inventory_cache_schedule.py', 'price_maintenance.py', 'price_schedule.py', 'profit_monitor.py', 'erp_price_monitor.py', 'profit_manifest.py', 'profit_rules.py', 'profit_panel.py',
                      'monitor_app_control.py', 'monitor_setup_service.py', 'monitor_setup_wizard.py',
                      'monitor_portable_install.py', 'inventory-setup.md', 'inventory-erp.user.js',
                      '.codex_tmp/inventory_monitor.spec', 'build_inventory_monitor.ps1') | ForEach-Object {
        [ordered]@{name = [IO.Path]::GetRelativePath($projectRoot, (Resolve-ProjectSource $_)); sha256 = (Get-FileHash -LiteralPath (Resolve-ProjectSource $_) -Algorithm SHA256).Hash}
    })
    # 同时记录网页源码及构建资源，候选包可追溯到本轮提交。
    console_files = @(@(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'app/web/src'),(Join-Path $projectRoot 'app/ui/console'),(Join-Path $projectRoot 'app/ui/console-host') -File -Recurse) | ForEach-Object {
        [ordered]@{name=[IO.Path]::GetRelativePath($projectRoot,$_.FullName);sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    })
    # 部门匹配兼容源只纳入公开运行代码，不携带运行配置或凭据。
    order_backend_files = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'integrations/order_backend') -File -Recurse | Where-Object { $_.Extension -in @('.py','.pyw','.ps1','.mjs') } | ForEach-Object {
        [ordered]@{name=[IO.Path]::GetRelativePath($projectRoot,$_.FullName);sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    })
    files = @(Get-ChildItem -LiteralPath $packageDirectory -File -Recurse | Where-Object {$_.Name -ne 'build-manifest.json'} | ForEach-Object {
        [ordered]@{name = [IO.Path]::GetRelativePath($packageDirectory, $_.FullName); size = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    })
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $packageDirectory 'build-manifest.json') -Encoding utf8
Write-Host "独立打包及归档验证通过：$packageDirectory"
