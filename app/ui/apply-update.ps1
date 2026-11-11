#requires -Version 7.0
param([Parameter(Mandatory)][string]$Plan)
$ErrorActionPreference='Stop'
# 只替换计划指定的单个 EXE，保留旧文件与业务数据库；失败回滚。
$p=Get-Content -LiteralPath $Plan -Raw | ConvertFrom-Json
$target=[IO.Path]::GetFullPath($p.target)
$source=[IO.Path]::GetFullPath($p.source)
$backup=$target+'.rollback-'+(Get-Date -Format 'yyyyMMddHHmmss')
$incoming=$target+'.incoming-'+[Guid]::NewGuid().ToString('N')
$replaced=$false
$oldExited=$false
function Start-Inventory([string[]]$ExtraArgs=@()) {
    # ArgumentList 逐项传递，中文路径及空格不会被 Start-Process 重新拆分。
    $startInfo=[Diagnostics.ProcessStartInfo]::new($target)
    $startInfo.UseShellExecute=$false
    $startInfo.CreateNoWindow=$true
    foreach($arg in @($p.arguments)+$ExtraArgs){if($null -ne $arg){$startInfo.ArgumentList.Add([string]$arg)}}
    return [Diagnostics.Process]::Start($startInfo)
}
try {
    if([IO.Path]::GetExtension($target) -ne '.exe' -or !(Test-Path -LiteralPath $target)){throw '目标程序无效'}
    # 唯一握手文件避免脚本被执行策略阻止时主程序误以为助手已接管。
    if($p.ready){'ready' | Set-Content -LiteralPath $p.ready}
    $old=Get-Process -Id $p.pid -ErrorAction SilentlyContinue
    if($old -and !$old.WaitForExit(120000)){throw '原程序未安全退出，取消更新'}
    $oldExited=$true
    if((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $p.sha256){throw '更新摘要不一致'}
    Copy-Item -LiteralPath $source -Destination $incoming
    # PyInstaller 的外层引导进程可能稍晚释放 EXE；仅重试文件占用，不结束任何进程。
    $replaceDeadline=(Get-Date).AddSeconds(30)
    while($true){
        try { [IO.File]::Replace($incoming,$target,$backup); break }
        catch [IO.IOException] {
            if((Get-Date) -ge $replaceDeadline){throw}
            Start-Sleep -Milliseconds 500
        }
    }
    $replaced=$true
    # 新版窗口与本地服务就绪后写入版本回执，再视为安装成功。
    $started=Start-Inventory -ExtraArgs @('--update-health',$p.marker)
    $deadline=(Get-Date).AddSeconds(90)
    $ok=$false
    while((Get-Date) -lt $deadline){
        if((Test-Path -LiteralPath $p.marker) -and (Get-Content -LiteralPath $p.marker -Raw).Trim() -eq $p.expected){$ok=$true;break}
        if($started.HasExited){break}
        Start-Sleep -Milliseconds 500
    }
    if(!$ok){throw '新版启动未确认，请查看更新记录'}
    '安装成功' | Set-Content -LiteralPath ($Plan+'.result')
} catch {
    # 仍存活的新进程可能正在处理业务，不能强杀或对它覆盖程序。
    if($replaced -and (!$started -or $started.HasExited)){
        [IO.File]::Replace($backup,$target,$target+'.failed-'+[Guid]::NewGuid().ToString('N'))
        $restored=Start-Inventory
    } elseif(!$replaced -and $oldExited) {
        # 摘要、复制或文件占用导致替换前失败，也要恢复仍完好的旧程序。
        $restored=Start-Inventory
    }
    $_.Exception.Message | Set-Content -LiteralPath ($Plan+'.result')
}
