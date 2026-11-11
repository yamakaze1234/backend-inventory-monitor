#requires -Version 7.0
param([string]$WebView2AssetsRoot)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$output=Join-Path $root 'app/ui/console-host'
New-Item -ItemType Directory -Path $output -Force | Out-Null
if(-not $WebView2AssetsRoot){
    # 固定官方 SDK 和摘要；构建不依赖作者电脑上另一个项目的 DLL。
    $sdkVersion='1.0.3537.50'
    $expectedHash='5EA526BBD728ADDA0DA4D31219267E96460494A427E4894C4E09D9F320F4B9AA'
    $cache=Join-Path $root '.build/webview2'
    $package=Join-Path $cache "microsoft.web.webview2.$sdkVersion.nupkg"
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    if(-not (Test-Path -LiteralPath $package)){
        Invoke-WebRequest -Uri "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$sdkVersion/microsoft.web.webview2.$sdkVersion.nupkg" -OutFile $package
    }
    if((Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash -ne $expectedHash){throw 'WebView2 SDK 摘要校验失败，请检查下载文件；未执行或解压该文件。'}
    # 只取三个明确的运行库文件，避免归档路径影响目标目录。
    $archive=[IO.Compression.ZipFile]::OpenRead($package)
    try {
        foreach($relative in @('lib/net462/Microsoft.Web.WebView2.Core.dll','lib/net462/Microsoft.Web.WebView2.WinForms.dll','runtimes/win-x64/native/WebView2Loader.dll')){
            $entry=$archive.GetEntry($relative)
            if(-not $entry){throw "WebView2 SDK 缺少文件：$relative"}
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,(Join-Path $output ([IO.Path]::GetFileName($relative))),$true)
        }
    } finally {$archive.Dispose()}
} else {
    # 保留显式 SDK 目录参数，供离线构建使用；不自动搜索其他项目。
    foreach($name in @('Microsoft.Web.WebView2.Core.dll','Microsoft.Web.WebView2.WinForms.dll','WebView2Loader.dll')){
        Copy-Item -LiteralPath (Join-Path $WebView2AssetsRoot $name) -Destination (Join-Path $output $name)
    }
}
$compiler=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if(-not (Test-Path -LiteralPath $compiler)){throw '缺少 .NET Framework 4.x 编译器，请先安装 Windows .NET Framework 开发组件。'}
& $compiler /nologo /target:winexe /platform:x64 "/out:$output/InventoryConsoleHost.exe" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll "/win32icon:$root/app/ui/monitor_assets/monitor.ico" "/reference:$output/Microsoft.Web.WebView2.Core.dll" "/reference:$output/Microsoft.Web.WebView2.WinForms.dll" (Join-Path $root 'app/src/InventoryConsoleHost.cs')
if($LASTEXITCODE){throw 'WebView2 桌面宿主构建失败'}
Write-Host 'WebView2 桌面宿主已构建'
