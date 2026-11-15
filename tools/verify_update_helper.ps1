#requires -Version 7.0
$ErrorActionPreference='Stop'
# 临时 EXE 只写验收回执；不会启动库存、匹配或钉钉进程。
$root=Join-Path (Split-Path $PSScriptRoot -Parent) ('outputs/订单整合验收/helper-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
$compiler=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$code=@'
using System;
using System.IO;
class TestApp {
  // 隔离替换测试使用固定版本回执，不调用任何业务逻辑。
  static void Main(string[] args) {
    if (Environment.GetEnvironmentVariable("PYINSTALLER_RESET_ENVIRONMENT") != "1")
      Environment.Exit(19);
    for(int i=0;i<args.Length-1;i++) if(args[i]=="--update-health") File.WriteAllText(args[i+1],"99.0");
    File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"started.txt"),string.Join("|",args));
  }
}
'@
$code | Set-Content -LiteralPath (Join-Path $root 'healthy.cs')
'class FailedApp { static void Main(string[] args) {if(System.Environment.GetEnvironmentVariable("PYINSTALLER_RESET_ENVIRONMENT")!="1")System.Environment.Exit(19);System.IO.File.WriteAllText(System.IO.Path.Combine(System.AppDomain.CurrentDomain.BaseDirectory,"started.txt"),string.Join("|",args));} }' | Set-Content -LiteralPath (Join-Path $root 'failed.cs')
& $compiler /nologo /target:winexe "/out:$root/healthy.exe" (Join-Path $root 'healthy.cs')
if($LASTEXITCODE){throw '验收程序编译失败'}
& $compiler /nologo /target:winexe "/out:$root/failed.exe" (Join-Path $root 'failed.cs')
if($LASTEXITCODE){throw '失败样例编译失败'}
$helper=Join-Path (Split-Path $PSScriptRoot -Parent) 'app/ui/apply-update.ps1'
foreach($case in @('success','rollback','bad-hash')){
    $folder=Join-Path $root $case
    New-Item -ItemType Directory -Path $folder | Out-Null
    $target=Join-Path $folder 'target.exe'
    $source=Join-Path $root $(if($case -eq 'rollback'){'failed.exe'}else{'healthy.exe'})
    Copy-Item -LiteralPath (Join-Path $root 'failed.exe') -Destination $target
    $oldHash=(Get-FileHash -LiteralPath $target).Hash
    $hash=(Get-FileHash -LiteralPath $source).Hash
    $plan=Join-Path $folder 'install.json'
    $ready=Join-Path $folder 'helper-ready.txt'
    @{pid=2147483646;target=$target;source=$source;sha256=$(if($case -eq 'bad-hash'){'0'*64}else{$hash});marker=(Join-Path $folder 'healthy.txt');ready=$ready;expected='99.0';arguments=@('--data-dir',(Join-Path $folder '中文 工作区'))} | ConvertTo-Json | Set-Content -LiteralPath $plan
    # 模拟旧冻结进程的环境，验证正常重启和回退都覆盖旧值。
    $resetBefore=$env:PYINSTALLER_RESET_ENVIRONMENT
    try {
        $env:PYINSTALLER_RESET_ENVIRONMENT='0'
        & pwsh.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $helper -Plan $plan
    } finally {
        $env:PYINSTALLER_RESET_ENVIRONMENT=$resetBefore
    }
    if(!(Test-Path -LiteralPath $ready)){throw '更新助手没有产生就绪握手'}
    $actual=(Get-FileHash -LiteralPath $target).Hash
    if($case -eq 'success' -and $actual -ne $hash){throw '成功替换验收失败'}
    if($case -ne 'success' -and $actual -ne $oldHash){throw "$case 未保留或恢复原程序"}
    $deadline=(Get-Date).AddSeconds(5)
    while(!(Test-Path -LiteralPath (Join-Path $folder 'started.txt')) -and (Get-Date) -lt $deadline){Start-Sleep -Milliseconds 100}
    $startedArgs=Get-Content -LiteralPath (Join-Path $folder 'started.txt') -Raw
    if(!$startedArgs.Contains('中文 工作区')){throw "$case 丢失自定义工作区"}
    Write-Host "$case 通过：$(Get-Content -LiteralPath ($plan+'.result'))"
}
