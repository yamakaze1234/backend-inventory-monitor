"""Console-free Task Scheduler entry point for the existing bot processes."""
import json
import os
import subprocess
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from order_department_runtime import settings as department_settings
DEPARTMENT = department_settings()
STATE = Path(DEPARTMENT['state_root'])
PWSH = DEPARTMENT['pwsh_path']
WORKER = Path(DEPARTMENT['match_root']) / 'app/src/group_order_bot.py'
sys.path.insert(0,str(WORKER.parent))
from robot_config import get_config
import harness_runtime
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def start(command, cwd, name):
    with (STATE / (name + '.stdout.log')).open('ab') as stdout, (STATE / (name + '.stderr.log')).open('ab') as stderr:
        child = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                 stdout=stdout, stderr=stderr, creationflags=NO_WINDOW)
    return child.pid


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    if (STATE / 'jobs.sqlite3').exists():
        with sqlite3.connect(STATE / 'jobs.sqlite3', timeout=15) as db:
            enabled = db.execute("SELECT value FROM settings WHERE key='serviceEnabled'").fetchone()
        if enabled and enabled[0] == '0':
            return
    config=get_config(STATE)
    # Harness 是唯一 Stream 接收者；DWS 仍可执行原有查询和通知投递。
    harness = harness_runtime.connector_mode(DEPARTMENT) == 'harness'
    script = r'''
    $p = @(Get-CimInstance Win32_Process)
    $w = @($p | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*'+$env:ORDER_BOT_MATCH_SCRIPT+'*') -and $_.CommandLine -match ' serve( |$)' })
    $c = @($p | Where-Object { $_.Name -eq 'dws.exe' -and $_.CommandLine -like '*dev connect*' -and $_.CommandLine -like '*APP_ID*' -and $_.CommandLine -notmatch 'connect (status|list|stop|restart)' })
    $foreign=@($c | Where-Object { $_.CommandLine -notlike ('*'+$env:ORDER_BOT_AGENT_ROOT+'*') })
    @{worker=($w.Count -gt 0);connector=($c.Count -gt 0);foreignConnector=($foreign.Count -gt 0)} | ConvertTo-Json -Compress
    '''.replace('APP_ID',config['appId'])
    os.environ['ORDER_BOT_MATCH_SCRIPT'] = str(WORKER)
    os.environ['ORDER_BOT_AGENT_ROOT'] = str(STATE/'agent')
    check = subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-Command', script],
                           capture_output=True, text=True, encoding='utf-8',
                           timeout=30, check=True, creationflags=NO_WINDOW)
    state = json.loads(check.stdout)
    if state.get('foreignConnector'):
        raise ValueError('该 Stream 机器人已有其他工作区连接，请使用本部门独立机器人')
    if not harness and DEPARTMENT.get('harness_entry') and DEPARTMENT.get('harness_home'):
        if harness_runtime.status(DEPARTMENT)['state']!='down':
            raise ValueError('旧 Harness 尚未停止，禁止启动第二条接收连接')
    if harness:
        if state['connector']:
            raise ValueError('请先停止同一机器人的旧 DWS Stream 连接')
        harness_runtime.start(DEPARTMENT, config, Path(os.environ['ORDER_BOT_CONFIG']).parent)
        state['connector']=harness_runtime.status(DEPARTMENT)['state'] != 'down'
    if not state['worker']:
        python = Path(DEPARTMENT['python_path'])
        if not python.is_file():
            raise RuntimeError('匹配运行环境不存在')
        state['startedWorker'] = start([str(python), '-B', '-X', 'utf8', str(WORKER), 'serve'], WORKER.parent, 'worker')
    if not harness and not state['connector']:
        state['startedConnector'] = start([PWSH, '-NoProfile', '-NonInteractive', '-File',
                                           str(ROOT / '启动机器人.ps1')], ROOT, 'connector')
    state['checkedAt'] = datetime.now().isoformat(timespec='seconds')
    (STATE / 'keepalive-status.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / 'keepalive-error.log').write_text(type(error).__name__ + ': ' + str(error), encoding='utf-8')
        raise
