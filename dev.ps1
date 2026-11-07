#requires -Version 7.0
[CmdletBinding()]
param([ValidateSet('Check','Test','Build','Preview','Run','WebBuild','WebPreview')][string]$Action='Check',
      [string]$PythonExecutable='D:\python\python.exe',
      [string]$OutputDirectory='outputs/候选包')
$ErrorActionPreference='Stop'
# 统一开发入口，现用数据仍由已安装程序管理。
$env:PYTHONPATH = (@('app/src','tests','.codex_tmp/tray_deps','beta/sql_inventory_20260917/deps') | ForEach-Object {Join-Path $PSScriptRoot $_}) -join ';'
Push-Location $PSScriptRoot
try {
    switch ($Action) {
        # 新界面预览必须指定隔离目录，不加载现用库存或订单队列。
        'WebBuild' { npm --prefix app/web run build }
        'WebPreview' { & $PythonExecutable -B -X utf8 app/src/inventory_console.py --preview-data (Join-Path $PSScriptRoot 'outputs/订单整合验收/preview-data') }
        'Check' { & $PythonExecutable -B -X utf8 tools/check_project.py }
        'Test' { & $PythonExecutable -B -X utf8 -m unittest discover -s tests -t .; if ($LASTEXITCODE -ne 0) {throw 'Python 测试失败'}; node --test tests/browser/*.cjs }
        'Build' { & ./tools/build_inventory_monitor.ps1 -PythonExecutable $PythonExecutable -OutputDirectory $OutputDirectory }
        'Preview' { & $PythonExecutable -B -X utf8 app/src/douyin_inventory_preview.py }
        'Run' { & ./app/runtime/启动现用程序.ps1 }
    }
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {throw "执行失败：$LASTEXITCODE"}
} finally { Pop-Location }
