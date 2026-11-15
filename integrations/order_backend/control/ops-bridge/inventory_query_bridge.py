"""本机运营机器人接收适配；只处理可信回调交给它的固定指令。"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

LOCAL = Path(__file__).resolve().parent
sys.path.insert(0, str(LOCAL / 'src' if (LOCAL/'src').is_dir() else LOCAL.parents[1]/'app/src'))
from ops_commands import parse_command, COMMANDS
from ops_receiver import LocalCommandClient, VerifiedMention
from ops_replies import render

def claim_error(ident, journal):
    # API 执行前拒绝通常没有业务回执，单独保留本地回复去重记录。
    with closing(sqlite3.connect(journal,timeout=5)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS replies(id TEXT PRIMARY KEY)')
        return db.execute('INSERT OR IGNORE INTO replies VALUES (?)',(ident,)).rowcount == 1


def run(raw, *, client=None, journal=None):
    # DWS 与 Harness 共用提交、回读和投递回执；调用方负责验证传输身份。
    if client is None:
        client = LocalCommandClient(os.environ['INVENTORY_QUERY_URL'], os.environ.pop('INVENTORY_QUERY_AUTH'))
    if journal is None:
        journal = LOCAL/'rejected-reply.sqlite3'
    if not isinstance(raw,dict) or set(raw) != {'Event', 'State'}:
        return {}
    payload, state = raw['Event'], raw['State']
    if not isinstance(payload,dict) or set(payload) != {'Org','Group','User','Robot','Message','Text'} or not all(isinstance(v,str) for v in payload.values()):
        return {}
    event = VerifiedMention(*(payload[k] for k in ('Org','Group','User','Robot','Message','Text')))
    if not event.message_id:
        return {}
    delivery = 'requests/' + quote(event.receipt, safe='') + '/delivery'
    if state in ('sent','unknown'):
        client.request(delivery, {'phase':'initial','state':state}, event.actor)
        return {}
    if state != 'prepare':
        return {}
    try:
        command, args = parse_command(event.text)
    except ValueError:
        prefix = event.text.strip().split(maxsplit=1)[0] if event.text.strip() else ''
        choices = [v[1] for v in COMMANDS.values() if v[0] == prefix]
        if choices and claim_error(event.receipt, journal):
            return dict(Text='请核对参数，例如「'+choices[0]+'」。', Receipt='')
        return {}
    if 'shop_alias' in args:
        args['shop_id'] = args.pop('shop_alias')
    result = client.request('requests', dict(request_id=event.receipt,command=command,actor=event.actor,args=args))
    if result.get('data',{}).get('_pending'):
        return {}
    response = client.request(delivery, {'phase':'initial','state':'claim'}, event.actor)
    if response.get('claimed') is True:
        return dict(Text=render(result), Receipt=event.receipt)
    if result.get('code') not in (None,'OK') and response.get('code') in ('NOT_FOUND','FORBIDDEN','COMMAND_DISABLED'):
        if claim_error(event.receipt, journal):
            return dict(Text=render(result), Receipt='')
    return {}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin)),ensure_ascii=False))
    except (KeyError,TypeError,ValueError,OSError):
        raise SystemExit(1) from None
