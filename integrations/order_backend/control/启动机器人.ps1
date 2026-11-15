#requires -Version 7.0
$ErrorActionPreference='Stop'
# 保留现用启动器的中文管道编码修复，避免读取机器人配置时解码失败。
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
# 部门配置由库存工作台保存，连接器只继承明确的运行上下文。
$configPath=$env:ORDER_BOT_CONFIG
if(-not $configPath){$configPath=(Get-Content -LiteralPath "$PSScriptRoot/order-department.json" -Raw | ConvertFrom-Json).configPath}
$env:ORDER_BOT_CONFIG=[IO.Path]::GetFullPath($configPath)
$department=Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$robotConfig=& $department.python_path -B -X utf8 "$PSScriptRoot/robot_desktop_control.py" config
if($LASTEXITCODE -ne 0){throw '读取订单机器人设置失败'}
$robotConfig=$robotConfig | ConvertFrom-Json
# 手工入口和工作台使用同一个启动器；Harness 模式不再启动 DWS Stream。
if ($department.connector_mode -eq 'harness') {
    & $department.python_path -B -X utf8 "$PSScriptRoot/harness_setup.py" Start
    exit $LASTEXITCODE
}
$agentRoot=Join-Path $department.state_root 'agent'
# 只有现用配置明确指向本机专用接收程序时，才注入已受保护的回环连接。
$workspace=Split-Path $env:ORDER_BOT_CONFIG -Parent
$receiver=Join-Path $workspace 'ops-query-runtime'
$pilotPath=Join-Path $workspace 'ops-command-pilot.json'
$bytes=$null
try {
    if([IO.Path]::GetFullPath($department.dws_path) -eq (Join-Path $receiver 'dws.exe') -and (Test-Path -LiteralPath $pilotPath)){
        $pilot=Get-Content -LiteralPath $pilotPath -Raw | ConvertFrom-Json
        $bridge=Join-Path $receiver 'inventory_query_bridge.py'
        if($pilot.robot_id -ne $robotConfig.robotCode -or ($robotConfig.profile -split ':')[0] -ne $pilot.org_id -or
           $pilot.port -lt 1024 -or $pilot.port -gt 65535 -or !(Test-Path -LiteralPath $bridge)){
            throw '专用机器人连接和当前部门设置不一致'
        }
        $encrypted=[IO.File]::ReadAllBytes((Join-Path $workspace 'ops-command-key.dat'))
        $bytes=[Security.Cryptography.ProtectedData]::Unprotect($encrypted,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
        $env:INVENTORY_QUERY_AUTH=[Text.Encoding]::ASCII.GetString($bytes)
        $env:INVENTORY_QUERY_ORG_ID=$pilot.org_id
        $env:INVENTORY_QUERY_GROUP_ID=$robotConfig.groupId
        $env:INVENTORY_QUERY_URL='http://127.0.0.1:'+($pilot.port)
        $env:INVENTORY_QUERY_PYTHON=$department.python_path
        $env:INVENTORY_QUERY_SCRIPT=$bridge
    }
    $modelArgs=@()
    if($department.agent_model){$modelArgs=@('--agent-model',$department.agent_model)}
    & $department.dws_path dev connect --unified-app-id $robotConfig.appId --channel codex @modelArgs --agent-workdir $agentRoot --allowed-groups $robotConfig.groupId --profile $robotConfig.profile --format json
    if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
} finally {
    if($bytes){[Array]::Clear($bytes,0,$bytes.Length)}
    [Environment]::SetEnvironmentVariable('INVENTORY_QUERY_AUTH',$null,'Process')
}
