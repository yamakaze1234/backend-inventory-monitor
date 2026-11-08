#requires -Version 7.0
$ErrorActionPreference='Stop'
# 部门配置由库存工作台保存，连接器只继承明确的运行上下文。
$configPath=$env:ORDER_BOT_CONFIG
if(-not $configPath){$configPath=(Get-Content -LiteralPath "$PSScriptRoot/order-department.json" -Raw | ConvertFrom-Json).configPath}
$env:ORDER_BOT_CONFIG=[IO.Path]::GetFullPath($configPath)
$department=Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$robotConfig=& $department.python_path -B -X utf8 "$PSScriptRoot/robot_desktop_control.py" config
if($LASTEXITCODE -ne 0){throw '读取订单机器人设置失败'}
$robotConfig=$robotConfig | ConvertFrom-Json
$agentRoot=Join-Path $department.state_root 'agent'
& $department.dws_path dev connect --unified-app-id $robotConfig.appId --channel codex --agent-model $department.agent_model --agent-workdir $agentRoot --allowed-groups $robotConfig.groupId --profile $robotConfig.profile --format json
