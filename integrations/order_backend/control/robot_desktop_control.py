"""Fixed desktop actions; no arbitrary commands or ERP parameters."""
import argparse
import contextlib
import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from order_department_runtime import settings as department_settings
DEPARTMENT = department_settings()
sys.path.insert(0, str(Path(DEPARTMENT['match_root']) / 'app/src'))
import group_order_bot as bot
from robot_record_cleanup import cleanup_due, cleanup_status
from robot_config import get_config
from robot_settings import discover, validated_target

PWSH = DEPARTMENT['pwsh_path']


def run(command, timeout=40):
    completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                               timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:
        raw = completed.stderr or completed.stdout or '命令未完成'
        for candidate in (completed.stdout, completed.stderr):
            try:
                value = json.loads(candidate)
                error = value.get('error', value)
                if isinstance(error, dict):
                    message = error.get('message') or error.get('errorMsg')
                    if message:
                        raw = str(message)
                        if error.get('hint'): raw += '；' + str(error['hint'])
                        break
                elif isinstance(error, str):
                    raw = error
                    break
            except (ValueError, TypeError, AttributeError):
                pass
        raise RuntimeError(bot.safe_error(' '.join(raw.split())))
    return completed.stdout


def settings(values, root=bot.STATE):
    with contextlib.closing(bot.queue_db(root)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', values.items())
        db.commit()


def ensure():
    module = runpy.run_path(str(ROOT / 'robot-keepalive.pyw'))
    module['main']()


def connector_status():
    config = get_config(bot.STATE)
    return json.loads(run([bot.DWS, 'dev', 'connect', 'status', '--robot-client-id', config['robotCode'],
                           '--profile', config['profile'], '--json', '--format', 'json']))


def startup(enabled=None):
    # 不修改其他部门的计划任务；此版本由库存软件显式启动、停止。
    if enabled is not None:raise ValueError('请通过部门独立的 Windows 启动项管理自启动')
    return False


def stop_connector():
    config = get_config(bot.STATE)
    state = connector_status()
    if state.get('state') == 'down':
        return True
    # 在调用按应用停止命令前确认工作区所属，不能停止另一部门的同应用连接。
    if not isinstance(state.get('pid'),int):return False
    os.environ['ORDER_BOT_AGENT_ROOT']=str(bot.STATE/'agent')
    os.environ['ORDER_BOT_DWS']=DEPARTMENT['dws_path']
    owned=run([PWSH,'-NoProfile','-NonInteractive','-Command',
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId="+str(state['pid'])+"'; if($p -and $p.ExecutablePath -eq $env:ORDER_BOT_DWS -and $p.CommandLine -like ('*'+$env:ORDER_BOT_AGENT_ROOT+'*')){'owned'}"])
    if owned.strip()!='owned':raise ValueError('连接器不属于本部门工作区，未停止')
    # The CLI stop command supports daemon records; Windows foreground Stream
    # connectors may report no daemon despite a live PID. Always read back.
    run([bot.DWS, 'dev', 'connect', 'stop', '--robot-client-id', config['robotCode'],
         '--profile', config['profile'], '--format', 'json', '--yes'])
    state = connector_status()
    if state.get('state') == 'down':
        return True
    push = state.get('lastPushAgoSec')
    reply = state.get('lastReplyAgoSec')
    if push is not None and (reply is None or push < reply):
        return False
    if state.get('supervised') or not isinstance(state.get('pid'), int):
        return False
    pid = state['pid']
    script = r'''
    $ErrorActionPreference='Stop'
    $all=@(Get-CimInstance Win32_Process)
    $target=$all | Where-Object { $_.ProcessId -eq TARGET_PID }
    if(-not $target){exit 0}
    if($target.Name -ne 'dws.exe' -or $target.ExecutablePath -ne $env:ORDER_BOT_DWS -or
       $target.CommandLine -notlike '*dev connect*APP_ID*') {
        throw '连接器进程身份不匹配，未停止'
    }
    $owned=New-Object 'System.Collections.Generic.List[object]'
    $owned.Add($target)
    for($i=0;$i -lt $owned.Count;$i++) {
        $parent=$owned[$i].ProcessId
        foreach($child in @($all | Where-Object { $_.ParentProcessId -eq $parent })) {
            if(-not @($owned | Where-Object { $_.ProcessId -eq $child.ProcessId }).Count) {$owned.Add($child)}
        }
    }
    foreach($item in $owned) {
        $live=Get-CimInstance Win32_Process -Filter ('ProcessId='+$item.ProcessId)
        if($live -and $live.CreationDate -eq $item.CreationDate) { Stop-Process -Id $item.ProcessId -ErrorAction Stop }
    }
    '''.replace('TARGET_PID', str(pid)).replace('APP_ID',config['appId'])
    os.environ['ORDER_BOT_DWS'] = DEPARTMENT['dws_path']
    run([PWSH, '-NoProfile', '-NonInteractive', '-Command', script])
    return connector_status().get('state') == 'down'


def pending_config(root):
    with contextlib.closing(bot.queue_db(root)) as db:
        row=db.execute("SELECT value FROM settings WHERE key='pendingRobotConfig'").fetchone()
    return json.loads(row[0]) if row and row[0] else None


def finish_switch(root):
    config=pending_config(root)
    if not config:return False
    if not handle('finish-stop',root).get('stopped'):return True
    with contextlib.closing(bot.queue_db(root)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',[
            ('robotConfig',json.dumps(config,ensure_ascii=False)),('pendingRobotConfig',''),
            ('serviceEnabled','1'),('stopRequested','0'),('lastNotification','')])
        db.commit()
    ensure()
    return True


def handle(action, root=bot.STATE, payload=None):
    # 发货提醒独立保存路由，沿用已校验的机器人及群选择；不切换匹配连接器。
    if action == 'shipping-config':
        from shipping_monitor import config
        return config(root)
    if action == 'shipping':
        return bot.enqueue('shipping', root=root)
    if action in ('shipping-on', 'shipping-off'):
        settings({'shippingEnabled': '1' if action == 'shipping-on' else '0'}, root)
        return {'message': '发货监控已启用' if action == 'shipping-on' else '发货监控已暂停'}
    if action in ('save-shipping-target', 'inherit-shipping-target'):
        target = validated_target(payload or {},run,bot.DWS) if action == 'save-shipping-target' else None
        with contextlib.closing(bot.queue_db(root)) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE kind='shipping' AND (state IN ('queued','running','unknown') OR delivery IN ('pending','unknown')) LIMIT 1").fetchone():
                raise ValueError('发货监控仍有在途或投递未知任务，请完成核验后再切换群')
            db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)', [('shippingTarget',json.dumps(target,ensure_ascii=False) if target else ''),('shippingNotification','')])
            db.commit()
        return {'message':'发货提醒目标已保存'}
    if action == 'config':return get_config(root)
    if action == 'discover-robots':
        return discover(payload or {},run,bot.DWS)
    if action == 'save-robot':
        config=validated_target(payload or {},run,bot.DWS)
        with contextlib.closing(bot.queue_db(root)) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM settings WHERE key='pendingRobotConfig' AND value<>''").fetchone():
                raise ValueError('已有切换正在进行，请稍后再保存')
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','unknown') OR delivery='unknown' OR (delivery='pending' AND state<>'running') LIMIT 1").fetchone():
                raise ValueError('存在排队或投递未确认的任务，请处理完成后再切换机器人')
            if not db.execute("SELECT 1 FROM settings WHERE key='robotConfig'").fetchone():
                # 新部门首次配置只保存路由，默认暂停，不能调用尚不存在的旧连接器。
                db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',[
                    ('robotConfig',json.dumps(config,ensure_ascii=False)),('serviceEnabled','0'),('stopRequested','1'),('schedulePaused','1'),('workerStopped','1')])
                db.commit()
                return {'message':'订单机器人已配置，尚未启动服务'}
            db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',[
                ('pendingRobotConfig',json.dumps(config,ensure_ascii=False)),('serviceEnabled','0'),('stopRequested','1')])
            db.commit()
        return {'message':'设置已保存，正在等待当前任务结束并切换连接。'}
    if action == 'start':
        if not finish_switch(root):
            settings({'serviceEnabled': '1', 'stopRequested': '0'}, root)
            ensure()
    elif action == 'ensure':
        if not finish_switch(root):ensure()
    elif action in ('pause', 'resume'):
        settings({'schedulePaused': '1' if action == 'pause' else '0'}, root)
    elif action == 'patrol':
        return bot.enqueue('today', root=root)
    elif action == 'cleanup':
        cleanup_due(root, force=True)
        return {'cleanup': cleanup_status(root)}
    elif action == 'stop':
        # Finish the running batch. Do not kill a process during ERP writes.
        settings({'serviceEnabled': '0', 'stopRequested': '1'}, root)
        return {'stopping': True}
    elif action == 'finish-stop':
        state = bot.status(root=root)
        if state['workerOnline']:
            return {'stopped': False, 'message': '等待当前匹配任务完成后退出'}
        if not state['workerStopped']:
            # A crashed worker cannot write a graceful-stop marker. Check both
            # supervisor and execution child before considering it stopped.
            os.environ['ORDER_BOT_MATCH_SCRIPT'] = str(Path(DEPARTMENT['match_root']) / 'app/src/group_order_bot.py')
            count = run([PWSH, '-NoProfile', '-NonInteractive', '-Command',
                         "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*group_order_bot.py*' -and $_.CommandLine -match ' (serve|execute)( |$)' -and $_.CommandLine -like ('*'+$env:ORDER_BOT_MATCH_SCRIPT+'*') }).Count"])
            if int(count.strip()) != 0:
                return {'stopped': False, 'message': '等待匹配工作进程结束'}
            settings({'workerStopped': '1'}, root)
        return {'stopped': stop_connector()}
    elif action in ('startup-on', 'startup-off'):
        return {'startupEnabled': startup(action == 'startup-on')}
    state = bot.status(root=root)
    if action in ('start', 'ensure'):
        try:
            cleanup_due(root)
        except (OSError, ValueError, sqlite3.Error):
            state['cleanup'] = {'error': '记录清理暂未完成，下次后台检查时重试'}
    if 'cleanup' not in state:
        state['cleanup'] = cleanup_status(root)
    try:
        state['connector'] = connector_status()
    except Exception as error:
        state['connector'] = {'state': 'unknown', 'detail': bot.safe_error(error)}
    try:
        state['startupEnabled'] = startup()
    except Exception:
        state['startupEnabled'] = None
    return state


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['snapshot', 'start', 'ensure', 'pause', 'resume',
                                         'patrol', 'shipping', 'shipping-config', 'shipping-on', 'shipping-off', 'save-shipping-target', 'inherit-shipping-target', 'cleanup', 'config', 'discover-robots', 'save-robot', 'stop', 'finish-stop', 'startup-on', 'startup-off'])
    args = parser.parse_args()
    try:
        payload=json.load(sys.stdin) if args.action in ('discover-robots','save-robot','save-shipping-target') else None
        print(json.dumps(handle(args.action,payload=payload), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': bot.safe_error(error)}, ensure_ascii=False))
        raise SystemExit(1)
