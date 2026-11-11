#requires -Version 5.1
<#
Windows PowerShell 5.1 仅用于新电脑引导；正常开发和更新使用 PowerShell 7。
默认只部署桌面运行环境；-Mode Development 另建项目独立的开发环境。
-CheckOnly 只检查，不安装、不建目录、不启动程序或业务。
#>
[CmdletBinding()]
param(
    [ValidateSet('Runtime','Development')][string]$Mode='Runtime',
    [switch]$IncludeBrowser,
    [switch]$CheckOnly
)
$ErrorActionPreference='Stop'
$environmentProjectRoot=Split-Path $PSScriptRoot -Parent

function Find-EnvironmentExecutable([string]$Name,[string[]]$Candidates=@()) {
    # PATH 可能在安装后尚未刷新，补查官方默认位置；不搜索用户配置目录。
    $command=Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if($command){return $command.Source}
    foreach($candidate in $Candidates){if($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)){return $candidate}}
    return $null
}

function Get-EnvironmentComponent([string]$Name) {
    switch($Name){
        'PowerShell' {
            $path=Find-EnvironmentExecutable 'pwsh.exe' @("$env:ProgramFiles/PowerShell/7/pwsh.exe")
            if($path){
                $version=& $path -NoProfile -NonInteractive -Command '$PSVersionTable.PSVersion.ToString()'
                if($LASTEXITCODE -eq 0 -and $version -match '^7\.\d+\.\d+$'){return $path}
            }
        }
        'WebView2' {
            # Evergreen Runtime 的注册表版本由微软安装器维护，与浏览器是否安装分开判断。
            foreach($hive in @('HKCU:','HKLM:')){
                foreach($prefix in @('Software/Microsoft/EdgeUpdate/Clients','Software/WOW6432Node/Microsoft/EdgeUpdate/Clients')){
                    $base=Join-Path $hive $prefix
                    foreach($key in @(Get-ChildItem -LiteralPath $base -ErrorAction SilentlyContinue)){
                        $entry=Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
                        if($entry.name -like '*WebView2*' -and $entry.pv -match '^\d+\.' -and $entry.pv -ne '0.0.0.0'){return [string]$entry.pv}
                    }
                }
            }
        }
        'Chrome' {
            return Find-EnvironmentExecutable 'chrome.exe' @("$env:ProgramFiles/Google/Chrome/Application/chrome.exe","${env:ProgramFiles(x86)}/Google/Chrome/Application/chrome.exe","$env:LOCALAPPDATA/Google/Chrome/Application/chrome.exe")
        }
        'Node' {
            $path=Find-EnvironmentExecutable 'node.exe' @("$env:ProgramFiles/nodejs/node.exe")
            if($path){$version=& $path --version; if($LASTEXITCODE -eq 0 -and $version -match '^v(\d+)\.' -and [int]$Matches[1] -ge 22){return $path}}
        }
        'Python' {
            # 固定 3.13 开发系列，避免误用 Windows 商店占位程序或不兼容的解释器。
            foreach($path in @("$env:LOCALAPPDATA/Programs/Python/Python313/python.exe","$env:ProgramFiles/Python313/python.exe",(Find-EnvironmentExecutable 'python.exe'))){
                if($path -and (Test-Path -LiteralPath $path) -and $path -notlike '*WindowsApps*'){
                    $version=& $path -c 'import sys; print("%s.%s" % sys.version_info[:2])'
                    if($LASTEXITCODE -eq 0 -and $version -eq '3.13'){return $path}
                }
            }
        }
    }
    return $null
}

function Install-EnvironmentComponent([string]$Name,[string]$PackageId) {
    $winget=Find-EnvironmentExecutable 'winget.exe'
    if(!$winget){throw '未找到 winget，请先从 Microsoft Store 安装或更新“应用安装程序”，再重新运行。'}
    Write-Host "正在安装 $Name（$PackageId）…"
    # 只使用 winget 官方源中的精确包标识；可能由系统提示管理员权限。
    & $winget install --id $PackageId --exact --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Host
    if($LASTEXITCODE -ne 0){throw "$Name 安装器返回 $LASTEXITCODE；请按安装器提示处理后重新运行。"}
    $env:PATH=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')+';'+$env:PATH
}

function Ensure-EnvironmentComponent([string]$Name,[string]$PackageId,[bool]$ReadOnly) {
    $installed=Get-EnvironmentComponent $Name
    if(!$installed -and !$ReadOnly){
        Install-EnvironmentComponent $Name $PackageId
        $installed=Get-EnvironmentComponent $Name
        if(!$installed){throw "$Name 安装后检测未通过。请重启终端后重新检查；尚不能判定环境已就绪。"}
    }
    $status=if($installed){'已就绪'}else{'缺失'}
    Write-Host "$Name : $status"
    return [bool]$installed
}

function Test-DevelopmentEnvironment([string]$Root) {
    # 以实际导入和前端锁文件为准，不把目录存在误判成完整安装。
    $python=Join-Path $Root '.venv/Scripts/python.exe'
    $npmLock=Join-Path $Root 'app/web/node_modules/.package-lock.json'
    if(!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $npmLock)){return $false}
    & $python -B -c 'import PIL, pystray, openpyxl, pymssql, websockets, pyxlsb, cryptography, PyInstaller' 2>$null
    return $LASTEXITCODE -eq 0
}

function Initialize-DevelopmentEnvironment([string]$Root) {
    $python=Get-EnvironmentComponent 'Python'
    $venv=Join-Path $Root '.venv'
    $venvPython=Join-Path $venv 'Scripts/python.exe'
    if(!(Test-Path -LiteralPath $venvPython)){
        & $python -m venv $venv | Out-Host
        if($LASTEXITCODE -ne 0){throw '创建项目独立 Python 环境失败。'}
    }
    & $venvPython -m pip install --requirement (Join-Path $Root 'requirements-build.txt') | Out-Host
    if($LASTEXITCODE -ne 0){throw 'Python 依赖安装失败。'}
    $node=Get-EnvironmentComponent 'Node'
    $npm=Join-Path (Split-Path $node -Parent) 'node_modules/npm/bin/npm-cli.js'
    if(!(Test-Path -LiteralPath $npm)){throw 'Node 安装中缺少 npm，请修复 Node.js 安装。'}
    & $node $npm ci --prefix (Join-Path $Root 'app/web') --no-audit --no-fund | Out-Host
    if($LASTEXITCODE -ne 0){throw '前端依赖安装失败。'}
    if(!(Test-DevelopmentEnvironment $Root)){throw '开发环境安装后导入检查失败。'}
}

function Invoke-EnvironmentPreparation([string]$Root,[string]$SelectedMode,[bool]$Browser,[bool]$ReadOnly) {
    if(![Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10 -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64'){throw '需要 Windows 10/11 x64；当前完整验收平台为 Windows 11。'}
    if($SelectedMode -eq 'Development'){
        foreach($relative in @('requirements-build.txt','app/web/package-lock.json')){
            if(!(Test-Path -LiteralPath (Join-Path $Root $relative))){throw '开发模式需要完整源码，请在源码仓库中运行。'}
        }
    }
    $components=@(@('PowerShell','Microsoft.PowerShell'),@('WebView2','Microsoft.EdgeWebView2Runtime'))
    if($Browser){$components+=,@('Chrome','Google.Chrome');$components+=,@('Node','OpenJS.NodeJS.LTS')}
    if($SelectedMode -eq 'Development'){
        if(!$Browser){$components+=,@('Node','OpenJS.NodeJS.LTS')}
        $components+=,@('Python','Python.Python.3.13')
    }
    $ready=$true
    foreach($component in $components){
        if(!(Ensure-EnvironmentComponent $component[0] $component[1] $ReadOnly)){$ready=$false}
    }
    if($SelectedMode -eq 'Development' -and $ready){
        if(!$ReadOnly){Initialize-DevelopmentEnvironment $Root}
        if(!(Test-DevelopmentEnvironment $Root)){Write-Host '项目 Python / 前端依赖：缺失';$ready=$false}else{Write-Host '项目 Python / 前端依赖：已就绪'}
    }
    if(!$ready){Write-Host '检测未通过，请运行部署模式补齐缺失组件。';return 2}
    Write-Host '环境检测通过。未启动库存采集、订单匹配或消息发送。'
    if($SelectedMode -eq 'Development'){Write-Host '开发入口：pwsh ./dev.ps1 Check -PythonExecutable ./.venv/Scripts/python.exe'}
    else{Write-Host '请使用启动.cmd 打开程序，再填写 ERP、店铺和机器人配置。'}
    return 0
}

# 点引用仅加载函数，便于在不安装系统软件的情况下运行隔离测试。
if($MyInvocation.InvocationName -ne '.'){
    try {exit (Invoke-EnvironmentPreparation $environmentProjectRoot $Mode ([bool]$IncludeBrowser) ([bool]$CheckOnly))}
    catch {Write-Host ('环境准备失败：'+$_.Exception.Message) -ForegroundColor Red;exit 1}
}
