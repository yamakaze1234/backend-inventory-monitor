"""管理本部门 Harness 实例及固定指令入口；不读取钉钉或模型登录资料。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
PACKAGE = '@dingtalk-real-ai/dsh-dingtalk'
VERSION = '0.10.2'
# 核对原版和用户提供的旧帮助版，只替换已知接入点，遇到未知版本不启动。
KNOWN = {
    '81458a5706e415b6a055a8798fba0c0e9ceac8d31925711b0079adee9e08f1cd',
    '505698e309499d32405c8ac20883c3d56f6017cd6ef86c90d7e69c89a779fc7e',
}
BEGIN = '// STOCK_MONITOR_HARNESS_ROUTE_V1'
LOAD = "async function startAccount(ctx, config, account, cwd, dwsTools) {\n"
ADMISSION = '    const digitalEmployees = validateDigitalEmployeeConfigs('
ROUTE = '            // Picture without vision support:'
OLD_HELP = "    const helpBridgePath = path.join(cwd, '..', 'ops-receiver', 'help.mjs');"
EXPECTED = 'process.env.STOCK_HARNESS_ROBOT'


def connector_mode(config):
    return config.get('connector_mode', 'codex')


def layout(config):
    control = Path(config['control_root']).resolve()
    home = Path(config['harness_home']).resolve()
    entry = Path(config['harness_entry']).resolve()
    return control, home, entry


def atomic(path, value):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.pending')
    temporary.write_text(value, encoding='utf-8')
    temporary.replace(path)


def patched(source):
    if BEGIN in source:
        if source.count(BEGIN) != 1:
            raise ValueError('钉钉插件接入点重复')
        return source
    if hashlib.sha256(source.encode()).hexdigest() not in KNOWN:
        raise ValueError('钉钉插件与已验证版本不一致，请安装受支持的 0.10.2')
    if OLD_HELP in source:
        start = source.index(OLD_HELP)
        source = source[:start] + source[source.index('    const { clientId, clientSecret } = account;', start):]
        start = source.index('            if (helpBridge && helpBridge.isHelp(msg)) {')
        source = source[:start] + source[source.index(ROUTE, start):]
    if source.count(LOAD) != 1 or source.count(ROUTE) != 1 or source.count(ADMISSION) != 1:
        raise ValueError('钉钉插件接入点不匹配')
    # 管理实例只允许一个已选企业机器人，避免同一进程接管其他配置或数字员工。
    gate = f"""    if ({EXPECTED} && (resolution.accounts.length !== 1 ||
        resolution.accounts[0].clientId !== {EXPECTED} ||
        (config.digitalEmployees ?? []).some(item => item.enabled)))
        throw new Error('Stock Monitor requires one matching robot and no digital employees');
"""
    source = source.replace(ADMISSION, gate + ADMISSION)
    source = source.replace(LOAD, LOAD + f"""    {BEGIN}
    const stockRoute = {EXPECTED} ? await import(process.env.STOCK_HARNESS_ROUTE_URL) : null;
    if (stockRoute) cwd = process.env.STOCK_HARNESS_WORKDIR;
""")
    status = '            streamStatus = status;'
    if source.count(status) != 1:
        raise ValueError('钉钉插件状态接入点不匹配')
    source = source.replace(status, status + """
            void stockRoute?.recordStatus(status, Boolean(currentDefault())).catch(() => {});
""")
    callback='        onMessage: async (msg) => {\n            const access = owner.authorize(msg);'
    if source.count(callback) != 1:
        raise ValueError('钉钉消息权限接入点不匹配')
    # 固定业务指令先交本机群/成员权限校验；普通 AI 对话仍遵循原群限制及 Harness 授权。
    return source.replace(callback, callback.replace('            const access', """            if (stockRoute && stockRoute.isFixed(msg.text)) {
                await queueAccessReply(msg.conversationId, () => stockRoute.routeFixed(msg, clientId,
                    text => stockRoute.sendFixedReply(outbound, msg.sessionWebhook, config.markdownTitle, text)));
                return;
            }
            if (stockRoute && msg.conversationType === 'group' && msg.conversationId !== process.env.STOCK_OPS_GROUP) return;
            const access"""))



def patch_stream(source):
    marker = '// STOCK_MONITOR_CALLBACK_IDENTITY_V1'
    if marker in source:
        if source.count(marker) != 1:
            raise ValueError('钉钉回调接入点重复')
        return source
    if hashlib.sha256(source.encode()).hexdigest() != '2b9e992726cbc8fd9bbd555a166d7d0f935c8e99ed4ab3f568d79221f890170d':
        raise ValueError('钉钉回调源码与已验证版本不一致')
    return source.replace('            await opts.onMessage({', f"""            await opts.onMessage({{
                {marker}
                sourceMsgId: typeof data?.msgId === 'string' ? data.msgId : '',
                senderCorpId: data?.senderCorpId,
                robotCode: data?.robotCode,
                isInAtList: data?.isInAtList === true && data?.msgtype === 'text',""")


def source_for_repair(asset, current):
    backup = asset.with_name(asset.name + '.stock-monitor-original')
    if BEGIN in current or 'STOCK_MONITOR_CALLBACK_IDENTITY_V1' in current:
        if not backup.is_file():
            raise ValueError('已修改的钉钉插件缺少原版备份，请重新安装固定版本')
        return backup, backup.read_text(encoding='utf-8')
    return backup, current


def install_hook(config):
    _, home, entry = layout(config)
    # Web profile 优先：setup 安装的依赖通常在这里，而不在便携 CLI 自身的依赖目录。
    runtime = entry.parents[4]
    harness = json.loads((entry.parent.parent/'package.json').read_text(encoding='utf-8'))
    if harness.get('version') != '0.2.2-rc.1':
        raise ValueError('当前兼容层只支持 Harness 0.2.2-rc.1')
    profile = home / 'profiles/web'
    packages = [p for base in (profile, runtime)
                if (p := base / 'node_modules' / PACKAGE / 'lib/index.js').is_file()]
    if not (profile/'node_modules'/PACKAGE/'lib/index.js').is_file():
        raise ValueError('Harness Web 尚未配置钉钉插件，请先运行 Configure')
    changes = {}
    for file in packages:
        target = file.resolve()
        if not any(target.is_relative_to(parent) for parent in (home, runtime)):
            raise ValueError('钉钉插件位于其他安装目录，未修改')
        if json.loads(target.parent.parent.joinpath('package.json').read_text(encoding='utf-8')).get('version') != VERSION:
            raise ValueError('Harness 钉钉插件需使用已验收版本 0.10.2')
        for asset, transform in ((target, patched), (target.with_name('stream.js'), patch_stream)):
            current = asset.read_text(encoding='utf-8')
            backup, original = source_for_repair(asset, current)
            updated = transform(original)
            if updated != current:
                if not backup.is_file() or backup.read_text(encoding='utf-8') != original:
                    changes[backup] = original
                changes[asset] = updated
    # 所有版本检查通过才写入，且不覆盖任何钉钉账户配置。
    for target, updated in changes.items():
        atomic(target, updated)


def paths(config):
    state = Path(config['state_root'])
    return tuple(state / ('harness-' + name) for name in ('process.json', 'health.json', 'stop.request'))


def inspect(config):
    """只返回本机相关 PID、创建时间和归属；不返回原始进程命令行。"""
    control, _, entry = layout(config)
    env = dict(os.environ, STOCK_PROBE_ENTRY=str(entry), STOCK_PROBE_WRAPPER=str(control/'harness-entry.mjs'),
               STOCK_PROBE_NODE=str(Path(config['node_path']).resolve()))
    script = r'''
$ErrorActionPreference='Stop'
$rows = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ForEach-Object {
    $line = $_.CommandLine
    if ($line -and ($line.IndexOf($env:STOCK_PROBE_ENTRY,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                    $line -match '(?i)[/\\]dsh[/\\]lib[/\\]bin\.js["\s]') -and $line -match '\sweb(\s|$)') {
        @{pid=$_.ProcessId; created=$_.CreationDate.ToUniversalTime().Ticks.ToString();
          owned=($_.ExecutablePath -eq $env:STOCK_PROBE_NODE -and
                 $line.IndexOf($env:STOCK_PROBE_ENTRY,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                 $line.IndexOf($env:STOCK_PROBE_WRAPPER,[StringComparison]::OrdinalIgnoreCase) -ge 0)}
    }
})
ConvertTo-Json -InputObject $rows -Compress
'''
    result = subprocess.run([config['pwsh_path'], '-NoProfile', '-NonInteractive', '-Command', script],
                            env=env, capture_output=True, encoding='utf-8', timeout=12,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('无法确认本机 Harness 进程归属')
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError('无法确认本机 Harness 进程归属')
    return rows


def read_public(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def status(config, rows=None):
    rows = inspect(config) if rows is None else rows
    if not rows:
        return dict(state='down', engine='harness')
    process, health, _ = paths(config)
    record = read_public(process)
    if len(rows) != 1 or not rows[0]['owned'] or rows[0]['pid'] != record.get('pid') or rows[0]['created'] != record.get('created'):
        return dict(state='unmanaged', engine='harness')
    latest = read_public(health)
    stream = latest.get('status') if latest.get('run') == record.get('run') else None
    return dict(state='healthy' if stream == 'connected' else 'degraded' if stream else 'starting',
                pid=record['pid'], engine='harness', modelReady=latest.get('modelReady') is True if stream else False)


def stop(config):
    current = status(config)
    if current['state'] == 'down':
        return True
    if current['state'] == 'unmanaged':
        return False
    process, _, request = paths(config)
    atomic(request, read_public(process)['run'])
    # 不强杀正在处理会话的进程，交给 Harness 自身的关闭流程。
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if status(config)['state'] == 'down':
            return True
        time.sleep(.2)
    return False


def dws_stopped(config, target):
    app = target.get('appId')
    if app:
        script = r'''
$ErrorActionPreference='Stop'
$live=@(Get-CimInstance Win32_Process -Filter "Name='dws.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine -match '\sdev\s+connect\b' -and
    $_.CommandLine -notmatch '\sconnect\s+(status|list|stop|restart)\b' -and
    $_.CommandLine.IndexOf($env:STOCK_DWS_APP,[StringComparison]::OrdinalIgnoreCase) -ge 0
})
$live.Count
'''
        probe = subprocess.run([config['pwsh_path'], '-NoProfile', '-NonInteractive', '-Command', script],
                               env=dict(os.environ, STOCK_DWS_APP=app), capture_output=True, encoding='utf-8',
                               timeout=12, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if probe.returncode or probe.stdout.strip() != '0':
            return False
    result = subprocess.run([config['dws_path'], 'dev', 'connect', 'status', '--robot-client-id', target['robotCode'],
                             '--profile', target['profile'], '--json', '--format', 'json'],
                            capture_output=True, encoding='utf-8', timeout=20,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        return False
    value = json.loads(result.stdout)
    return (not result.returncode and isinstance(value, dict) and not value.get('error')
            and value.get('state') in ('down', 'not_running') and value.get('pid') is None)


def start(config, target, workspace):
    control, home, entry = layout(config)
    process, health, request = paths(config)
    org, sep, user = target['profile'].partition(':')
    if not sep or not org or not user or not target['robotCode'].startswith('ding') or not target['groupId'].startswith('cid'):
        raise ValueError('请先配置并核对钉钉组织、机器人及接收群')
    # 锁跨越探测和启动，防止保活、桌面和迁移入口并发创建连接。
    with closing(sqlite3.connect(process.parent/'harness-start-lock.sqlite3', timeout=5)) as lock:
        lock.execute('BEGIN IMMEDIATE')
        current = status(config)
        if current['state'] == 'unmanaged':
            raise ValueError('请先从原启动入口停止已有 Harness Web')
        if not dws_stopped(config, target):
            raise ValueError('请先停止同一机器人的旧 DWS Stream 连接')
        if current['state'] != 'down':
            return None
        from harness_bridge import probe
        try:
            probe(workspace, org, target['robotCode'])
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise ValueError('运营指令接口未就绪，请重启库存监控并核对工作区') from error
        install_hook(config)
        run = uuid4().hex
        bins = entry.parents[4]/'node_modules/.bin'
        env = dict(os.environ, DSH_HOME=str(home), STOCK_HARNESS_ROBOT=target['robotCode'],
                   STOCK_OPS_ORG=org, STOCK_OPS_GROUP=target['groupId'], STOCK_OPS_PYTHON=config['python_path'],
                   STOCK_OPS_WORKSPACE=str(workspace), STOCK_HARNESS_ROUTE_URL=(control/'harness-route.mjs').as_uri(),
                   STOCK_HARNESS_WORKDIR=str(control), STOCK_HARNESS_RUN=run, STOCK_HARNESS_HEALTH=str(health), STOCK_HARNESS_STOP=str(request),
                   PATH=os.pathsep.join((str(Path(config['node_path']).parent),str(bins),os.environ.get('PATH',''))))
        # 不把回环密钥交给 Harness 或模型进程；Python 指令桥按需在本机解密。
        for key in ('INVENTORY_QUERY_AUTH', 'INVENTORY_OPS_TOKEN'):
            env.pop(key, None)
        # DSH 启动输出可能包含 Web 登录地址，不转存输出；健康检查仅回读结构化状态。
        child = subprocess.Popen([config['node_path'], str(control/'harness-entry.mjs'), str(entry), 'web', '--no-open', '--host', '127.0.0.1'],
                                 cwd=control, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        matches = [row for row in inspect(config) if row['pid'] == child.pid and row['owned']]
        if len(matches) != 1:
            atomic(request, run)
            raise ValueError('Harness 启动后未能确认进程归属')
        try:
            atomic(process, json.dumps(dict(run=run, **matches[0])))
        except OSError:
            atomic(request, run)
            raise
        return child.pid
