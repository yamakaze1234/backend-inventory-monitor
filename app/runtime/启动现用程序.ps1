#requires -Version 7.0
# 优先启动构建产物；没有产物时从源码启动。
$root=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$exe=Join-Path $root 'outputs/v1.06/后台库存监控_v1.06.exe'
if (Test-Path -LiteralPath $exe) {Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe) -WindowStyle Hidden}
else { & python (Join-Path $root 'app/src/inventory_app.py') }
