#requires -Version 7.0
param([string]$PackageRoot, [switch]$CheckOnly)
$ErrorActionPreference='Stop'
try {
    if (!$PackageRoot) {$PackageRoot=Read-Host '请输入新机原一体迁移包根目录（包含 inventory、robot、settings）'}
    $PackageRoot=$PackageRoot.Trim('"')
    # 从原工作区选用现有 Python，不安装环境、不复制作者配置。
    $preferences=Join-Path $PackageRoot 'device-paths.json'
    $workspace=Join-Path $PackageRoot 'workspace'
    if(Test-Path -LiteralPath $preferences){
        $selected=(Get-Content -LiteralPath $preferences -Raw | ConvertFrom-Json).workspace
        if($selected){$workspace=$selected}
    }
    $config=Get-Content -LiteralPath (Join-Path $workspace 'order-bot.json') -Raw | ConvertFrom-Json
    if(!(Test-Path -LiteralPath $config.python_path)){throw '原配置中的 Python 不存在，请核对所选一体包。'}
    $arguments=@('-X','utf8',(Join-Path $PSScriptRoot 'repair_new_machine.py'),'--package-root',$PackageRoot)
    if($CheckOnly){$arguments+='--check'}
    & $config.python_path @arguments
    if($LASTEXITCODE -ne 0){throw '修复未完成，请按上面的原因处理后重试。'}
    Write-Host '校验完成。修复成功后可直接打开原库存 EXE；机器人与计划保持原状态。'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
