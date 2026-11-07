#requires -Version 7.0
param([Parameter(Mandatory)][string]$Candidate)
$ErrorActionPreference='Stop'
# 只启动候选的隔离样例模式，不打开桌面窗口或加载生产数据。
$candidatePath=(Resolve-Path $Candidate).Path
$sample=Join-Path (Get-Location) ('outputs/订单整合验收/package-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sample -Force | Out-Null
$process=Start-Process -FilePath $candidatePath -ArgumentList @('--console-preview-data',('"'+$sample+'"'),'--console-headless','--console-preview-seconds','45') -WindowStyle Hidden -PassThru
$deadline=[DateTime]::UtcNow.AddSeconds(30)
while (!(Test-Path (Join-Path $sample 'preview-url.txt'))) {
    if ($process.HasExited -or [DateTime]::UtcNow -gt $deadline) {throw '候选隔离服务未正常启动'}
    Start-Sleep -Milliseconds 250
}
$url=Get-Content (Join-Path $sample 'preview-url.txt') -Raw
node tools/verify_console_ui.cjs $url
if ($LASTEXITCODE -ne 0) {throw '候选前端验收失败'}
if (!$process.WaitForExit(60000)) {throw '候选未按隔离验收时限退出'}
$process.Refresh()
if ($process.ExitCode -ne 0) {throw "候选退出异常：$($process.ExitCode)"}
$runtime=Get-Content (Join-Path $sample 'host-check.txt') -Raw
[ordered]@{candidate=$candidatePath;sha256=(Get-FileHash $candidatePath -Algorithm SHA256).Hash;webview2_runtime=$runtime.Trim();exit_code=$process.ExitCode;desktop_opened=$false;production_data_used=$false;verified_at=(Get-Date -Format o)} | ConvertTo-Json | Set-Content outputs/订单整合验收/package-verification.json -Encoding utf8
Get-Content outputs/订单整合验收/package-verification.json
