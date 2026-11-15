"""现有运营机器人接收程序的接入件；不创建第二个 Stream 连接。"""
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from ops_command_api import ACTOR, PREFIX, canonical, envelope
from ops_commands import COMMANDS, parse_command
from ops_replies import render


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalCommandClient:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('指令接口必须使用固定本机回环地址')
        self.url, self.token = url.rstrip('/'), token

    def request(self, suffix, body=None, actor=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        if actor:
            headers.update({'X-Actor-' + k.replace('_','-').title(): v for k, v in actor.items()})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.url + PREFIX + suffix, data=canonical(body).encode() if body is not None else None, headers=headers)
        try:
            # 回环请求不经机器代理，凭证只送往本机工作台。
            from urllib.request import ProxyHandler
            response = build_opener(ProxyHandler({}), NoRedirect).open(request, timeout=110)
        except HTTPError as error:
            response = error
        with response:
            return json.loads(response.read(2_000_000))


@dataclass(frozen=True)
class VerifiedMention:
    """只能由原接收器验证真实群 @ 和应用身份后构造，不能从群文本推断。"""
    org_id: str
    group_id: str
    user_id: str
    robot_id: str
    message_id: str
    text: str

    @property
    def actor(self):
        return {k: getattr(self, k) for k in ACTOR}

    @property
    def receipt(self):
        return 'dingtalk:' + hashlib.sha256(canonical((self.org_id, self.message_id)).encode()).hexdigest()


class Receiver:
    def __init__(self, client, robot_id, journal, sender):
        self.client, self.robot_id, self.journal, self.sender = client, robot_id, journal, sender

    def _track(self, event):
        # 只保存任务回复所需的稳定身份，不保存原群文本。
        with closing(sqlite3.connect(self.journal, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, org TEXT, grp TEXT, usr TEXT, event TEXT, checked INTEGER NOT NULL DEFAULT 0)')
            db.execute('INSERT OR IGNORE INTO tasks(id,org,grp,usr,event) VALUES (?,?,?,?,?)',
                       (event.receipt, event.org_id, event.group_id, event.user_id, event.message_id))

    def tick(self, limit=50):
        # 原接收器可用自身调度定期回读；重启后继续使用同一账本。
        with closing(sqlite3.connect(self.journal, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, org TEXT, grp TEXT, usr TEXT, event TEXT, checked INTEGER NOT NULL DEFAULT 0)')
            rows = db.execute('SELECT id,org,grp,usr,event FROM tasks ORDER BY checked,rowid LIMIT ?', (max(1, min(limit, 100)),)).fetchall()
        for receipt, org, group, user, message in rows:
            event = VerifiedMention(org, group, user, self.robot_id, message, '')
            try:
                state = self.finish(event)
            except (OSError, ValueError):
                state = None
            with closing(sqlite3.connect(self.journal, timeout=10)) as db, db:
                if state in ('sent', 'failed', 'unknown', 'suppressed'):
                    db.execute('DELETE FROM tasks WHERE id=?', (receipt,))
                else:
                    db.execute('UPDATE tasks SET checked=? WHERE id=?', (time.time_ns(), receipt))

    def _send(self, event, phase, receipt, text):
        # 本地投递账本覆盖参数和权限错误。发送结果不明时保留 unknown，不自动重发。
        with closing(sqlite3.connect(self.journal, timeout=10)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS reply (id TEXT, phase TEXT, state TEXT, PRIMARY KEY(id,phase))')
                if db.execute("INSERT OR IGNORE INTO reply VALUES (?,?,'unknown')", (receipt, phase)).rowcount != 1:
                    return 'unknown'
            try:
                state = self.sender(event, text)
                if state not in ('sent', 'failed', 'unknown'):
                    state = 'unknown'
            except Exception:
                state = 'unknown'
            with db:
                db.execute('UPDATE reply SET state=? WHERE id=? AND phase=?', (state, receipt, phase))
        return state

    def _deliver(self, event, reply, phase):
        receipt = event.receipt
        if reply['data'].get('_pending'):
            return None
        text = render(reply)
        suffix = 'requests/' + quote(receipt, safe='') + '/delivery'
        claimed = self.client.request(suffix, {'phase': phase, 'state': 'claim'}, event.actor)
        if claimed.get('claimed') is True:
            actual = claimed.get('reply')
            state = self._send(event, phase, receipt, render(actual) if actual else text)
            self.client.request(suffix, {'phase': phase, 'state': state}, event.actor)
            return state
        if phase == 'final' and claimed.get('done') is True:
            return 'suppressed'
        if phase == 'initial' and reply['code'] != 'OK' and claimed.get('code') in ('NOT_FOUND', 'COMMAND_DISABLED', 'FORBIDDEN', 'SERVICE_UNAVAILABLE'):
            return self._send(event, phase, receipt, text)
        return None

    def receive(self, event):
        if not isinstance(event, VerifiedMention) or event.robot_id != self.robot_id or not event.message_id:
            return None
        try:
            command, args = parse_command(event.text)
        except ValueError:
            prefix = event.text.split(maxsplit=1)[0] if event.text.strip() else ''
            command = next((k for k, (label, _) in COMMANDS.items() if label == prefix), '')
            if command:
                text = f'【{prefix}｜请补全参数】例如「{COMMANDS[command][1]}」。'
            else:
                text = '【指令｜未识别】请核对指令格式。'
            self._send(event, 'initial', event.receipt, text)
            return None
        if 'shop_alias' in args:
            args['shop_id'] = args.pop('shop_alias')
        reply = self.client.request('requests', dict(request_id=event.receipt, command=command, actor=event.actor, args=args))
        if command in ('order.match', 'order.patrol') and reply['code'] == 'OK':
            self._track(event)
        self._deliver(event, reply, 'initial')
        return reply

    def finish(self, event):
        """由原接收器在任务结束时用已持久化的原事件调用。"""
        if not isinstance(event, VerifiedMention) or event.robot_id != self.robot_id or not event.message_id:
            return None
        return self._deliver(event, envelope(event.receipt, 'order.match', 'unknown'), 'final')
