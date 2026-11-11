#requires -Version 7.0
param([string]$PythonExecutable)
$ErrorActionPreference='Stop'
# 优先启动构建产物；没有产物时从源码启动。
$root=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if(-not $PythonExecutable){
    $localPython=Join-Path $root '.venv/Scripts/python.exe'
    $PythonExecutable=if(Test-Path -LiteralPath $localPython){$localPython}else{'python'}
}
# 直接读取唯一版本声明，避免启动旧版本，也不要求已有包的用户安装 Python。
$versionText=Get-Content -LiteralPath (Join-Path $root 'app/src/inventory_version.py') -Raw
if($versionText -notmatch "(?m)^VERSION\s*=\s*'([0-9.]+)'"){throw '版本声明无效'}
$version=$Matches[1]
$exe=Join-Path $root "outputs/v$version/后台库存监控_v$version.exe"
if (Test-Path -LiteralPath $exe) {Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe) -WindowStyle Hidden}
else { & $PythonExecutable (Join-Path $root 'app/src/inventory_app.py'); if($LASTEXITCODE){throw '源码启动失败'} }
