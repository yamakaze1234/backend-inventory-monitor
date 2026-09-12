"""Shared robot configuration and durable per-recipient delivery receipts.

Only encrypted credentials are persisted. Public methods return safe metadata.
Each job snapshots its audience once; paused/new recipients never get a backlog.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

CHANNELS = {'inventory': '库存提醒', 'daily': '货源日报', 'messages': '钉钉货源消息'}


class RobotStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'notification_robots.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS robots (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, secret BLOB NOT NULL,
                    enabled INTEGER NOT NULL, channels TEXT NOT NULL,
                    since TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    fingerprint TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS jobs (
                    channel TEXT, job TEXT, occurred REAL, PRIMARY KEY(channel,job));
                CREATE TABLE IF NOT EXISTS receipts (
                    channel TEXT, job TEXT, robot TEXT, name TEXT, status TEXT,
                    epoch REAL, retryable INTEGER DEFAULT 0, updated REAL,
                    PRIMARY KEY(channel,job,robot));
            ''')
            if 'fingerprint' not in [r[1] for r in db.execute('PRAGMA table_info(robots)')]:
                db.execute("ALTER TABLE robots ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
        self.migrate('legacy-messages', self.root / 'delivery.dat', '原货源消息机器人', ['messages'])

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def migrate(self, ident, path, name, channels):
        # Copy existing DPAPI ciphertext internally, without exposing plaintext.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM migrations WHERE id=?', (ident,)).fetchone() or not Path(path).is_file():
                return
            secret = Path(path).read_bytes()
            db.execute('INSERT OR IGNORE INTO robots VALUES (?,?,?,?,?,?,0,?)',
                       (ident, name, secret, 1, json.dumps(channels),
                        json.dumps({c: 0 for c in channels}), secret_fingerprint(secret)))
            db.execute('INSERT INTO migrations VALUES (?)', (ident,))

    def migrate_environment(self):
        """Legacy standalone installs only; callers must opt in explicitly."""
        import os
        from monitor_secret_store import protect
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM migrations WHERE id IN ('legacy-messages','legacy-environment')").fetchone():
                return
            value = os.environ.get('DINGTALK_WEBHOOK', '')
            if not value:
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                        value = winreg.QueryValueEx(key, 'DINGTALK_WEBHOOK')[0]
                except (OSError, ImportError):
                    return
            if not value:
                return
            from monitor_setup_service import validate_webhook
            validate_webhook(value)
            fingerprint = hashlib.sha256(value.strip().encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO robots VALUES (?,?,?,?,?,?,0,?)',
                       ('legacy-environment', '原货源消息机器人', protect(value.strip().encode()), 1,
                        json.dumps(['messages']), json.dumps({'messages': 0}), fingerprint))
            db.execute("INSERT INTO migrations VALUES ('legacy-environment')")

    def list_robots(self):
        with self.db() as db:
            return [dict(id=r[0], name=r[1], enabled=bool(r[2]), channels=json.loads(r[3]))
                    for r in db.execute('SELECT id,name,enabled,channels FROM robots WHERE deleted=0 ORDER BY rowid')]

    def save(self, name, webhook='', channels=None, robot_id=None, enabled=True):
        name = str(name).strip()
        channels = sorted(set(channels or []))
        if not name or len(name) > 60:
            raise ValueError('请输入机器人备注名称（1～60 个字符）。')
        if not channels or any(c not in CHANNELS for c in channels):
            raise ValueError('请至少选择一种通知。')
        encrypted = None
        if webhook.strip():
            from monitor_setup_service import validate_webhook
            from monitor_secret_store import protect
            validate_webhook(webhook)
            encrypted = protect(webhook.strip().encode('utf-8'))
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT secret,enabled,since,fingerprint FROM robots WHERE id=? AND deleted=0', (robot_id,)).fetchone()
            if robot_id is not None and old is None:
                raise ValueError('机器人不存在，请刷新列表。')
            if old is None and encrypted is None:
                raise ValueError('新增机器人需要填写完整 Webhook 地址。')
            since = json.loads(old[2]) if old else {}
            since = {c: since.get(c, now) if old and old[1] and enabled and encrypted is None else now for c in channels}
            ident = robot_id or uuid.uuid4().hex
            fingerprint = hashlib.sha256(webhook.strip().encode()).hexdigest() if encrypted is not None else old[3]
            for other_id, scopes, other_fingerprint in db.execute('SELECT id,channels,fingerprint FROM robots WHERE deleted=0'):
                if other_id != ident and fingerprint and fingerprint == other_fingerprint and set(channels).intersection(json.loads(scopes)):
                    raise ValueError('同一机器人已接收所选通知，请编辑已有机器人，避免重复推送。')
            db.execute('INSERT OR REPLACE INTO robots VALUES (?,?,?,?,?,?,0,?)',
                       (ident, name, encrypted if encrypted is not None else old[0], int(bool(enabled)),
                        json.dumps(channels), json.dumps(since), fingerprint))
        return ident

    def set_enabled(self, robot_id, enabled):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT enabled,channels,since FROM robots WHERE id=? AND deleted=0', (robot_id,)).fetchone()
            if row is None:
                raise ValueError('机器人不存在，请刷新列表。')
            since = {c: time.time() for c in json.loads(row[1])} if enabled and not row[0] else json.loads(row[2])
            db.execute('UPDATE robots SET enabled=?,since=? WHERE id=?', (int(bool(enabled)), json.dumps(since), robot_id))

    def delete(self, robot_id):
        # Logical removal keeps delivery receipts and migration tombstones.
        with self.db() as db:
            db.execute("UPDATE robots SET deleted=1,enabled=0,secret=x'' WHERE id=?", (robot_id,))

    def recent_receipts(self, limit=30):
        with self.db() as db:
            return [dict(channel=r[0], name=r[1], status='unknown' if r[2] == 'sending' and time.time() - r[3] > 60 else r[2], at=r[3]) for r in db.execute(
                'SELECT channel,name,status,updated FROM receipts ORDER BY updated DESC LIMIT ?', (limit,))]

    def send(self, channel, job, payload, occurred_at, transport=None):
        if channel not in CHANNELS:
            raise ValueError('未知通知类型。')
        transport = transport or send_encrypted
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            exists = db.execute('SELECT 1 FROM jobs WHERE channel=? AND job=?', (channel, job)).fetchone()
            if not exists:
                db.execute('INSERT INTO jobs VALUES (?,?,?)', (channel, job, occurred_at))
                for ident, name, enabled, scopes, since in db.execute(
                        'SELECT id,name,enabled,channels,since FROM robots WHERE deleted=0').fetchall():
                    epochs = json.loads(since)
                    if channel not in json.loads(scopes):
                        continue
                    epoch = epochs[channel]
                    status = 'pending' if enabled and occurred_at >= epoch else 'skipped'
                    db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?,0,?)',
                               (channel, job, ident, name, status, epoch, time.time()))
            targets = [r[0] for r in db.execute(
                "SELECT robot FROM receipts WHERE channel=? AND job=? AND (status='pending' OR (status='failed' AND retryable=1)) ORDER BY rowid",
                (channel, job))]
        for ident in targets:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                receipt = db.execute('SELECT status,epoch,retryable FROM receipts WHERE channel=? AND job=? AND robot=?',
                                     (channel, job, ident)).fetchone()
                if receipt[0] != 'pending' and not (receipt[0] == 'failed' and receipt[2]):
                    continue
                robot = db.execute('SELECT enabled,channels,since,deleted,secret FROM robots WHERE id=?', (ident,)).fetchone()
                eligible = (robot and robot[0] and not robot[3] and channel in json.loads(robot[1])
                            and json.loads(robot[2]).get(channel) == receipt[1])
                db.execute('UPDATE receipts SET status=?,retryable=0,updated=? WHERE channel=? AND job=? AND robot=?',
                           ('sending' if eligible else 'skipped', time.time(), channel, job, ident))
            if not eligible:
                continue
            try:
                result = transport(robot[4], payload)
                status = result.get('status', 'unknown')
                if status not in ('sent', 'failed', 'unknown'):
                    status = 'unknown'
                retryable = status == 'failed' and result.get('retryable') is True
            except Exception:
                status, retryable = 'unknown', False
            with self.db() as db:
                db.execute('UPDATE receipts SET status=?,retryable=?,updated=? WHERE channel=? AND job=? AND robot=?',
                           (status, int(retryable), time.time(), channel, job, ident))
        with self.db() as db:
            recipients = [dict(id=r[0], name=r[1], status='unknown' if r[2] == 'sending' else r[2], retryable=bool(r[3]))
                          for r in db.execute('SELECT robot,name,status,retryable FROM receipts WHERE channel=? AND job=? ORDER BY rowid', (channel, job))]
        statuses = {r['status'] for r in recipients if r['status'] != 'skipped'}
        retryable = any(r['retryable'] for r in recipients)
        status = ('skipped' if not statuses else 'sent' if statuses == {'sent'} else
                  'partial' if 'sent' in statuses or (retryable and 'unknown' in statuses) else
                  'unknown' if 'unknown' in statuses else 'failed')
        return dict(status=status, recipients=recipients, retryable=retryable)


def store_for_service(service):
    with service.db() as db:
        root = service.get(db, 'daily_source_root', str(service.root))
    store = RobotStore(root)
    store.migrate('legacy-inventory', service.root / 'inventory_robot.dat', '原库存与日报机器人', ['inventory', 'daily'])
    return store


def secret_fingerprint(secret):
    from monitor_secret_store import unprotect
    try:
        return hashlib.sha256(unprotect(secret).strip()).hexdigest()
    except Exception:
        # An unreadable legacy credential remains visible as a configuration;
        # delivery will safely fail until the user replaces its address.
        return ''


def send_encrypted(secret, payload):
    import urllib.request
    from monitor_secret_store import unprotect
    try:
        webhook = unprotect(secret).decode('utf-8')
    except Exception:
        return dict(status='failed', retryable=False)
    try:
        request = urllib.request.Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                         headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
        if type(result.get('errcode')) is int:
            return dict(status='sent' if result['errcode'] == 0 else 'failed', retryable=False)
    except Exception:
        pass
    return dict(status='unknown', retryable=False)


def forward_source(root, source, event, payload):
    from suhao_cost_monitor import _get
    raw = _get(event, 'createTime')
    try:
        occurred = float(raw)
        if occurred > 1e11:
            occurred /= 1000
        if not 0 < occurred <= time.time() + 300:
            raise ValueError()
    except (ValueError, TypeError):
        try:
            date = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            occurred = date.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() if date.tzinfo is None else date.timestamp()
        except (ValueError, TypeError):
            # No reliable source time: do not risk forwarding an old event on resume.
            occurred = 0
    key = hashlib.sha256(json.dumps([source.get('Profile', ''), _get(event, 'conversationId'),
                                    _get(event, 'messageId')], ensure_ascii=False).encode()).hexdigest()
    return RobotStore(root).send('messages', key, payload, occurred)
