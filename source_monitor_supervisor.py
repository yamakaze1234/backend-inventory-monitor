"""Wait for the desktop client, then supervise three future-event subscriptions.

Status contains no message bodies or credentials. DWS stdout is consumed in memory.
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from suhao_cost_monitor import process_lines, _get

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'monitor_runtime'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
DWS_EXECUTABLE = None
PORTABLE_MODE = False


def load_sources():
    try:
        content = (ROOT / 'monitor_sources.json').read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        # First launch on a new computer precedes the setup wizard.
        return []
    return json.loads(content)


def load_delivery_environment():
    protected = ROOT / 'delivery.dat'
    if protected.is_file():
        from monitor_secret_store import unprotect
        os.environ['DINGTALK_WEBHOOK'] = unprotect(protected.read_bytes()).decode('utf-8')
        return
    if PORTABLE_MODE:
        raise RuntimeError('请先完成通知配置。')
    # Reuse existing user environment at runtime; never log credential values.
    if not os.environ.get('DINGTALK_WEBHOOK'):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            os.environ['DINGTALK_WEBHOOK'] = winreg.QueryValueEx(key, 'DINGTALK_WEBHOOK')[0]


def has_dingtalk(output):
    return any(row and row[0].lower() == 'dingtalk.exe'
               for row in csv.reader(io.StringIO(output)))


def client_running():
    result = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq DingTalk.exe', '/FO', 'CSV', '/NH'],
                            capture_output=True, creationflags=NO_WINDOW, timeout=15)
    return result.returncode == 0 and has_dingtalk(result.stdout.decode(errors='replace'))


class Worker:
    def __init__(self, source):
        self.source = source
        self.thread = None
        self.status = 'waiting_for_dingtalk'
        self.pid = None
        self.next_retry = 0
        self.attempts = 0
        self.proc = None
        self.stop_requested = threading.Event()
        self.inventory_service = None

    def record_inventory_message(self, event):
        if self.inventory_service is None:
            from inventory_monitor import InventoryService
            self.inventory_service = InventoryService(ROOT)
        self.inventory_service.record_message(dict(
            message_id=_get(event, 'messageId'), text=_get(event, 'text'),
            created_at=_get(event, 'createTime'),
            sender=_get(event, 'sender') or self.source.get('SenderName', ''),
            group=_get(event, 'conversationName') or self.source.get('GroupName', ''),
            sender_id=_get(event, 'senderId'), group_id=_get(event, 'conversationId')))

    def is_alive(self):
        return (self.thread is not None and self.thread.is_alive()) or time.monotonic() < self.next_retry

    def start(self):
        self.stop_requested.clear()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_requested.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self.status = 'stopping'
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        elif self.thread is None or not self.thread.is_alive():
            self.status = 'paused'
            self.next_retry = 0

    def run(self):
        self.attempts += 1
        self.status = 'connecting'
        proc = None
        try:
            dws = DWS_EXECUTABLE or shutil.which('dws.exe')
            if not dws:
                self.status = 'dws_unavailable'
                return
            command = [dws, 'event', '+listen-im', '--kind', 'group',
                '--chat-id', self.source['GroupId'],
                '--events', 'message', '--format', 'ndjson', '--duration', '0']
            if self.source.get('Profile'):
                command += ['--profile', self.source['Profile']]
            proc = subprocess.Popen(command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW, cwd=ROOT)
            self.pid = proc.pid
            self.proc = proc
            if self.stop_requested.is_set():
                self.stop()

            def read_status():
                for line in proc.stderr:
                    if '[event] ready ' in line:
                        self.status = 'ready'
                    elif 'error' in line.lower() or 'failed' in line.lower():
                        self.status = 'connection_error'
                    # Deliberately do not persist arbitrary stderr or subscription IDs.

            threading.Thread(target=read_status, daemon=True).start()

            def lines():
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            event.setdefault('conversationName', self.source['GroupName'])
                            if _get(event, 'senderId') == self.source['SenderId']:
                                event['sender'] = self.source.get('SenderName', '')
                            yield json.dumps(event, ensure_ascii=False)
                    except (ValueError, TypeError):
                        continue

            from notification_robots import forward_source
            process_lines(lines(), webhook='',
                sender=lambda event, payload: forward_source(ROOT, self.source, event, payload),
                sources=((self.source['GroupId'], self.source['SenderId']),),
                state_path=ROOT / self.source['State'],
                terms=tuple(self.source['Terms'].split(',')),
                allow_reopen=self.source['AllowReopen'], on_event=self.record_inventory_message)
            self.status = 'disconnected'
        except Exception as error:
            self.status = 'error_' + type(error).__name__
        finally:
            if proc:
                # Closing piped stdin requests graceful DWS subscription cleanup.
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.status = 'waiting_for_listener_exit'
                    proc.wait()  # No duplicate subscription while the old one lives.
                self.pid = None
                self.proc = None
            if self.stop_requested.is_set():
                self.status = 'paused'
            self.next_retry = 0 if self.stop_requested.is_set() else time.monotonic() + 30


class Supervisor:
    def __init__(self, sources, factory=Worker):
        self.workers = [factory(source) for source in sources]
        self.activated = False

    def tick(self, dingtalk_running, enabled=True, force_start=False):
        if not enabled:
            self.activated = False
            for worker in self.workers:
                worker.stop()
            return
        self.activated = self.activated or dingtalk_running or force_start
        if self.activated:
            for worker in self.workers:
                if not worker.is_alive():
                    worker.start()


def main(root=None, dws_executable=None):
    global ROOT, RUNTIME, DWS_EXECUTABLE, PORTABLE_MODE
    if root is not None:
        ROOT = Path(root).resolve()
        RUNTIME = ROOT / 'monitor_runtime'
        DWS_EXECUTABLE = dws_executable
        PORTABLE_MODE = True
    import msvcrt
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # OS releases this lock when the supervisor exits, including abnormal exits.
    lock = (RUNTIME / 'supervisor.lock').open('a+b')
    if os.fstat(lock.fileno()).st_size == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return 0
    active_sources = load_sources()
    manager = Supervisor(active_sources)
    from notification_robots import RobotStore
    store = RobotStore(ROOT)  # Robot changes are read per event.
    if not PORTABLE_MODE:
        store.migrate_environment()
    while True:
        try:
            control = json.loads((RUNTIME / 'control.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            control = {'enabled': True}
        enabled = control.get('enabled', True)
        candidate_sources = load_sources()
        if candidate_sources != active_sources:
            manager.tick(False, enabled=False)
            if all(not worker.is_alive() for worker in manager.workers):
                active_sources = candidate_sources
                manager = Supervisor(active_sources)
            else:
                enabled = False
        enabled = enabled and bool(active_sources)
        manager.tick(client_running() if enabled and not manager.activated else False,
                     enabled=enabled, force_start=control.get('force_until', 0) > time.time())
        status = {'robot_protocol': 1, 'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'pid': os.getpid(),
                  'heartbeat': time.time(), 'enabled': enabled, 'configured': bool(active_sources),
                  'activated': manager.activated, 'sources': [
                      {'group': w.source['GroupName'], 'key': w.source['State'], 'status': w.status,
                       'pid': w.pid, 'attempts': w.attempts} for w in manager.workers]}
        temporary = RUNTIME / 'status.tmp'
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows readers/antivirus can briefly hold status.json without delete
        # sharing. A missed heartbeat must not terminate all message listeners.
        for attempt in range(5):
            try:
                temporary.replace(RUNTIME / 'status.json')
                break
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
        time.sleep(2)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        RUNTIME.mkdir(exist_ok=True)
        with (RUNTIME / 'supervisor_errors.log').open('a', encoding='utf-8') as log:
            log.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + type(error).__name__ + '\n')
        raise SystemExit(1)
