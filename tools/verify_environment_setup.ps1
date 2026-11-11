#requires -Version 7.0
$ErrorActionPreference='Stop'
# 隔离替换组件探测和安装器，验证控制流程；不安装软件、不改系统或业务配置。
. (Join-Path $PSScriptRoot 'prepare-environment.ps1')
$script:installed=@{}
$script:installCalls=@()
$script:probeCalls=@()
$script:developmentCalls=0
$script:installationWorks=$true
function Get-EnvironmentComponent([string]$Name){
    $script:probeCalls+=,$Name
    if($script:installed[$Name]){return 'mock-installed'}
    return $null
}
function Install-EnvironmentComponent([string]$Name,[string]$PackageId){
    $script:installCalls+=,$PackageId
    if($script:installationWorks){$script:installed[$Name]=$true}
}
function Initialize-DevelopmentEnvironment([string]$Root){$script:developmentCalls++}
function Test-DevelopmentEnvironment([string]$Root){return $script:developmentCalls -gt 0}
function Assert([bool]$Condition,[string]$Message){if(!$Condition){throw $Message}}
$root=Split-Path $PSScriptRoot -Parent
$result=Invoke-EnvironmentPreparation $root 'Runtime' $false $true
Assert ($result -eq 2) '缺失依赖时 CheckOnly 应返回 2'
Assert ($script:installCalls.Count -eq 0) 'CheckOnly 不得安装软件'
Assert ($script:probeCalls -notcontains 'Python') '运行模式不得要求 Python'

$result=Invoke-EnvironmentPreparation $root 'Runtime' $false $false
Assert ($result -eq 0) '组件安装并回读成功后才返回成功'
Assert (($script:installCalls -join ',') -eq 'Microsoft.PowerShell,Microsoft.EdgeWebView2Runtime') '基础模式应只安装两个必需组件'
$count=$script:installCalls.Count
$result=Invoke-EnvironmentPreparation $root 'Runtime' $false $false
Assert ($script:installCalls.Count -eq $count) '重复运行不得重装已就绪组件'

$result=Invoke-EnvironmentPreparation $root 'Runtime' $true $false
Assert ($script:installed['Chrome'] -and $script:installed['Node']) '抖店选项应补齐浏览器和 Node'
Assert (!$script:installed['Python']) '浏览器选项不需要 Python'

$result=Invoke-EnvironmentPreparation $root 'Development' $false $true
Assert ($result -eq 2 -and $script:developmentCalls -eq 0) '开发检查不得安装或创建虚拟环境'
$result=Invoke-EnvironmentPreparation $root 'Development' $false $false
Assert ($result -eq 0 -and $script:developmentCalls -eq 1) '开发模式应创建并验证依赖'

$script:installed=@{}
$script:installationWorks=$false
$failed=$false
try{Ensure-EnvironmentComponent 'PowerShell' 'Microsoft.PowerShell' $false | Out-Null}catch{$failed=$true}
Assert $failed '安装器返回后探测仍缺失时必须失败'
Write-Host '环境部署隔离验证通过：只读、安装后回读、幂等、可选浏览器、开发模式、失败处理。'
