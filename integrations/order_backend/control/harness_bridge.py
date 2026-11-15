"""Harness 固定指令桥。每次调用独立核验，登录凭据不进入 Harness 环境。"""
import json
import os
from pathlib import Path
import sys

LOCAL = Path(__file__).resolve().parent / 'ops-bridge'
sys.path[:0] = [str(LOCAL / 'src'), str(LOCAL)]
from inventory_query_bridge import run
from monitor_secret_store import unprotect
from ops_command_api import checked_id
from ops_receiver import LocalCommandClient, VerifiedMention


def connection(workspace):
    root = Path(workspace)
    pilot = json.loads((root/'ops-command-pilot.json').read_text(encoding='utf-8-sig'))
    org, robot, port = (pilot[key] for key in ('org_id', 'robot_id', 'port'))
    if not checked_id(org) or not checked_id(robot) or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError
    key = root/'ops-command-key.dat'
    if not 0 < key.stat().st_size <= 16384:
        raise ValueError
    return org, robot, LocalCommandClient('http://127.0.0.1:'+str(port),
                                          unprotect(key.read_bytes()).decode('ascii'), timeout=4)


def probe(workspace, org, robot):
    configured_org, configured_robot, client = connection(workspace)
    if (configured_org, configured_robot) != (org, robot) or client.request('capabilities').get('version') != 'commands/v1':
        raise ValueError('运营指令接口未就绪，请重启库存监控并核对工作区')


def dispatch(raw):
    root = Path(os.environ['STOCK_OPS_WORKSPACE'])
    org, robot, client = connection(root)
    event = raw['Event']
    if (event['Org'], event['Robot'], event['Group']) != (org, robot, os.environ['STOCK_OPS_GROUP']):
        return {}
    if any(not checked_id(event[k]) for k in ('Org', 'Group', 'User', 'Robot')) or not event['Message']:
        return {}
    client.timeout = 105
    identity = VerifiedMention(*(event[k] for k in ('Org','Group','User','Robot','Message','Text')))
    if raw['State'] == 'prepare' and client.request('member', actor=identity.actor).get('member') is not True:
        return {}
    # 拒绝回执账本归工作区，更新兼容文件后继续去重。
    return run(raw, client=client, journal=root/'ops-harness-replies.sqlite3')


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.load(sys.stdin)), ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit(1) from None
