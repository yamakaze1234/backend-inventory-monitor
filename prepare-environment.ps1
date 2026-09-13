# Compatible with Windows PowerShell 5.1 on recipient PCs; prefer PowerShell 7.
[CmdletBinding()]
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) {
        throw '需要 Windows 10/11 64 位；Win7 不支持。Windows 10 尚未整套实测。'
    }
    $runtimeRoot = Join-Path $PSScriptRoot 'runtime'
    $nodePath = Join-Path $runtimeRoot 'node-v24.16.0-win-x64/node.exe'
    $cliPath = Join-Path $runtimeRoot 'opencli/node_modules/@jackwener/opencli/dist/src/main.js'
    if ($CheckOnly) {
        Write-Host ('Node runtime ready: ' + (Test-Path -LiteralPath $nodePath))
        Write-Host ('OpenCLI runtime ready: ' + (Test-Path -LiteralPath $cliPath))
        if (!(Test-Path -LiteralPath $nodePath) -or !(Test-Path -LiteralPath $cliPath)) { exit 2 }
        & $nodePath $cliPath doctor
        exit $LASTEXITCODE
    }
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    if (!(Test-Path -LiteralPath $nodePath)) {
        Write-Host '正在从 Node.js 官网下载便携运行环境，无需修改系统 Node.js…'
        $filename = 'node-v24.16.0-win-x64.zip'
        $download = Join-Path $runtimeRoot $filename
        $baseUrl = 'https://nodejs.org/dist/v24.16.0/'
        Invoke-WebRequest -UseBasicParsing -Uri ($baseUrl + $filename) -OutFile $download
        $hashes = (Invoke-WebRequest -UseBasicParsing -Uri ($baseUrl + 'SHASUMS256.txt')).Content
        $expected = (($hashes -split "`n" | Where-Object { $_.Trim().EndsWith('  ' + $filename) }) -split '\s+')[0]
        if ($expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $expected) {
            throw 'Node 下载校验失败，未解压或执行。请检查网络后重试。'
        }
        Expand-Archive -LiteralPath $download -DestinationPath $runtimeRoot -Force
    }
    $taskOriginalPath = $env:PATH
    try {
        $env:PATH = (Split-Path -Parent $nodePath) + ';' + $env:PATH
        if (!(Test-Path -LiteralPath $cliPath)) {
            Write-Host '正在安装固定版本 OpenCLI 1.8.6 到程序自己的 runtime 目录…'
            $npmScript = Join-Path (Split-Path -Parent $nodePath) 'node_modules/npm/bin/npm-cli.js'
            & $nodePath $npmScript install --prefix (Join-Path $runtimeRoot 'opencli') --registry=https://registry.npmjs.org --omit=dev --no-audit --no-fund '@jackwener/opencli@1.8.6'
            if ($LASTEXITCODE -ne 0) { throw 'OpenCLI 安装未完成，请检查 npm 网络连接后重试。' }
        }
        if (!(Test-Path -LiteralPath $cliPath)) { throw 'OpenCLI 文件不完整。' }
        & $nodePath $cliPath --version
        if ($LASTEXITCODE -ne 0) { throw 'OpenCLI 无法启动。' }
        Write-Host '运行环境已准备。还需要在浏览器手动确认安装扩展，登录 ERP 并安装采集脚本。'
        & $nodePath $cliPath doctor
        if ($LASTEXITCODE -ne 0) { Write-Host '浏览器尚未连接，请按完整教程安装并连接 OpenCLI 扩展后再次检查。' }
    } finally { $env:PATH = $taskOriginalPath }
    $guide = Join-Path $PSScriptRoot '完整使用教程.html'
    if (Test-Path -LiteralPath $guide) { Start-Process -FilePath $guide -WindowStyle Hidden }
    Write-Host '准备流程结束。完成浏览器配置后再开启本机自动重登。'
} catch {
    Write-Host ('准备失败：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
