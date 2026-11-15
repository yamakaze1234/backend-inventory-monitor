"""Credential-free OpenCLI login clicks, bound to a verified ERP collector tab."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

SESSION = 'inventory-monitor-native'
ORIGIN = 'https://cqzs.3cerp.com'
PROBE = '''(() => {const b=document.querySelector('[data-inventory-client]');
const p=document.querySelector('#password'),u=document.querySelector('#username');
const v=document.querySelector('#verifydiv'),c=document.querySelector('#verifycode');
const m=document.querySelector('#msg');const login=document.querySelector('#loginbtn');
return {origin:location.origin,path:location.pathname,client:b?.dataset.inventoryClient||'',
captcha:!!(v?.getClientRects().length||c?.getClientRects().length),
filled:!!(p?.matches(':-webkit-autofill')&&u?.matches(':-webkit-autofill')),
rejected:!!(m?.getClientRects().length&&/密码.*(?:错误|不正确)|(?:账号|账户|用户).*(?:错误|不存在|锁定|禁用)|登录.*频繁/.test(m.innerText)),
requesting:!!(window.jQuery&&window.jQuery.active>0),
button:!!(login?.getClientRects().length&&!login.disabled)}})()'''


class OpenCli:
    def __init__(self, folder=None):
        self.node = shutil.which('node') or str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'nodejs/node.exe')
        self.cli = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules/@jackwener/opencli/dist/src/main.js'
        if folder is not None:
            runtime = Path(folder) / 'runtime'
            portable_node = runtime / 'node-v24.16.0-win-x64/node.exe'
            portable_cli = runtime / 'opencli/node_modules/@jackwener/opencli/dist/src/main.js'
            if portable_node.is_file() and portable_cli.is_file():
                self.node, self.cli = str(portable_node), portable_cli

    def run(self, *args):
        if not self.cli.is_file():
            raise RuntimeError('browser_dependency_missing')
        result = subprocess.run([self.node, str(self.cli), 'browser', SESSION, *args],
            capture_output=True, encoding='utf-8', errors='replace', timeout=18,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # Commands are fixed probes/actions. Never retain raw CLI diagnostics.
        if result.returncode:
            raise RuntimeError('browser_command_failed')
        try:
            value, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())
        except ValueError:
            raise RuntimeError('browser_response_invalid') from None
        if not isinstance(value, dict) or value.get('error'):
            raise RuntimeError('browser_response_invalid')
        return value


class BrowserRecovery:
    def __init__(self, service, cli=None):
        self.service = service
        self.cli = cli or OpenCli(service.root.parent)
        self.stop_event = threading.Event()
        self.thread = None
        self.bound = False

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='erp-browser-recovery')
        self.thread.start()
        return self

    def save(self, state):
        with self.service.db() as db:
            self.service.put(db, 'erp_native_state', state)

    def run_once(self, now=None):
        if hasattr(self.service, 'collector_enabled') and not self.service.collector_enabled():
            return
        now = time.time() if now is None else now
        with self.service.db() as db:
            request = self.service.get(db, 'erp_native_request', {})
            previous = self.service.get(db, 'erp_native_state', {})
            enabled = self.service.get(db, 'enabled', False)
            enabled = enabled and self.service.get(db, 'erp_native_enabled', False)
        if not enabled or not request.get('enabled') or now-request.get('at', 0)>25:
            return
        if now-previous.get('checked_at', 0)<5:
            return
        state = dict(previous, checked_at=now)
        try:
            if not self.bound:
                # Binding does not focus, navigate, or close the user's tab.
                self.cli.run('bind')
                self.bound = True
            page = self.cli.run('eval', PROBE)
            if page.get('origin') != ORIGIN or page.get('client') != request.get('client_id'):
                self.cli.run('unbind')
                self.bound = False
                state['status'] = 'wrong_tab'
                self.save(state)
                return
            if page.get('path') != '/login.jsp':
                # Collection (not page navigation) confirms actual recovery.
                if previous.get('attempts') and not previous.get('collection_requested'):
                    with self.service.db() as db:
                        if self.service.get(db, 'enabled', False):
                            self.service.put(db, 'next_check', 0)
                    state['collection_requested'] = True
                state['status'] = 'connected'
                self.save(state)
                return
            if previous.get('status') == 'connected':
                state.update(attempts=0, last_click=0, collection_requested=False)
            if page.get('captcha') or page.get('rejected'):
                state['status'] = 'manual_required'
            elif request.get('state') != 'native_ready' or not page.get('filled'):
                state['status'] = 'waiting_fill'
            elif page.get('requesting') or now-state.get('last_click', 0)<10:
                state['status'] = 'waiting_result'
            elif state.get('attempts', 0)>=3:
                state['status'] = 'exhausted'
            elif page.get('button'):
                target = self.cli.run('find', '--css', '#loginbtn', '--text-max', '0')
                entries = target.get('entries', [])
                if target.get('matches_n') != 1 or len(entries)!=1 or not entries[0].get('visible'):
                    raise RuntimeError('login_button_unavailable')
                # Recheck the tab identity immediately before the only write.
                check = self.cli.run('eval', PROBE)
                if (check.get('origin'),check.get('path'),check.get('client')) != (ORIGIN,'/login.jsp',request['client_id']):
                    raise RuntimeError('page_changed')
                if check.get('captcha') or check.get('rejected') or check.get('requesting') or not check.get('filled'):
                    return
                # Durable claim prevents duplicate clicks after a process restart.
                with self.service.db() as db:
                    if not self.service.get(db,'erp_native_enabled',False) or not self.service.get(db,'enabled',False):
                        return
                state.update(status='clicking', attempts=state.get('attempts',0)+1, last_click=now)
                self.save(state)
                self.cli.run('click', str(entries[0]['ref']))
                state['status'] = 'waiting_result'
            self.save(state)
            if state.get('status') in ('manual_required','exhausted'):
                with self.service.db() as db:
                    self.service.put(db, 'erp_recovery', {'state':'exhausted','at':now})
        except Exception:
            state['status'] = 'browser_unavailable'
            self.bound = False
            self.save(state)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                pass
            self.stop_event.wait(2)

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
        if self.bound and not (self.thread and self.thread.is_alive()):
            try:
                self.cli.run('unbind')
            except Exception:
                pass
