#requires -Version 7.0
# 统一同步入口：锁文件过期即失败，所有开发命令使用同一个项目环境。
function Initialize-UvEnvironment([string]$Root,[string]$PythonExecutable) {
    $uv=Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if(-not $uv){throw '缺少 uv，请先运行 tools/一键部署环境.cmd -Mode Development。'}
    $syncArgs=@('sync','--locked','--project',$Root)
    if($PythonExecutable){$syncArgs+=@('--python',$PythonExecutable)}
    & $uv.Source @syncArgs | Out-Host
    if($LASTEXITCODE -ne 0){throw 'uv 锁定环境同步失败，请检查 uv.lock 与 Python 版本。'}
    # 与构建器返回的实际项目解释器保持一致，不退回系统 Python。
    $selected=& $uv.Source run --locked --no-sync --project $Root python -X utf8 -c 'import sys; print(sys.executable)'
    if($LASTEXITCODE -ne 0){throw 'uv 项目解释器不可用'}
    return [string]$selected
}

