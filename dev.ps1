#requires -Version 7.0
[CmdletBinding()]
param([ValidateSet('Check','Test','Build','Preview','Run')][string]$Action='Check',
      [string]$PythonExecutable='python',
      [string]$OutputDirectory='outputs/候选包')
$ErrorActionPreference='Stop'
# 统一开发入口，现用数据仍由已安装程序管理。
$env:PYTHONPATH = (@('app/src','tests') | ForEach-Object {Join-Path $PSScriptRoot $_}) -join ';'
Push-Location $PSScriptRoot
try {
    switch ($Action) {
        'Check' { & $PythonExecutable -B -X utf8 tools/check_project.py }
        'Test' { & $PythonExecutable -B -X utf8 -m unittest discover -s tests -t .; if ($LASTEXITCODE -ne 0) {throw 'Python 测试失败'}; node --test tests/browser/*.cjs }
        'Build' { & ./tools/build_inventory_monitor.ps1 -PythonExecutable $PythonExecutable -OutputDirectory $OutputDirectory }
        'Preview' { & $PythonExecutable -B -X utf8 app/src/douyin_inventory_preview.py }
        'Run' { & ./app/runtime/启动现用程序.ps1 }
    }
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {throw "执行失败：$LASTEXITCODE"}
} finally { Pop-Location }
