#requires -Version 7.0
param([string]$WebView2AssetsRoot='D:\工作\匹配\抖店SKU查询\抖店SKU查询\app\runtime')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$output=Join-Path $root 'app/ui/console-host'
New-Item -ItemType Directory -Path $output -Force | Out-Null
# 只复制 WebView2 SDK 静态运行库，不接触匹配程序或浏览器登录目录。
foreach($name in @('Microsoft.Web.WebView2.Core.dll','Microsoft.Web.WebView2.WinForms.dll','WebView2Loader.dll')){
    Copy-Item -LiteralPath (Join-Path $WebView2AssetsRoot $name) -Destination (Join-Path $output $name)
}
$compiler=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:winexe /platform:x64 "/out:$output/InventoryConsoleHost.exe" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/reference:$output/Microsoft.Web.WebView2.Core.dll" "/reference:$output/Microsoft.Web.WebView2.WinForms.dll" (Join-Path $root 'app/src/InventoryConsoleHost.cs')
if($LASTEXITCODE){throw 'WebView2 桌面宿主构建失败'}
Write-Host 'WebView2 桌面宿主已构建'
