"""First-run setup. DWS authentication stays with DWS on the recipient computer."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse, parse_qs

from monitor_app_control import read_json, write_json
from monitor_secret_store import protect

SUPPLY_TERMS = '缺货,库存,价格,调价,降价,提货,货源,到货,报价,涨价,供货'
DEFAULT_ROWS = [
    dict(GroupName='示例销售管理群', SenderName='示例供货员',
         Terms='重开成本表,成本表重新开,重新开表,成本重新开,成本表,调价,降价,价格,价格更新', AllowReopen=True),
    dict(GroupName='示例整机业务群', SenderName='示例采购员', Terms=SUPPLY_TERMS, AllowReopen=False),
    dict(GroupName='示例销售协同群', SenderName='示例采购员', Terms=SUPPLY_TERMS, AllowReopen=False),
    dict(GroupName='示例运营群', SenderName='示例运营员', Terms=SUPPLY_TERMS, AllowReopen=False),
]


class SetupError(Exception):
    pass


class ChoiceRequired(SetupError):
    def __init__(self, title, candidates):
        super().__init__(title)
        self.title = title
        self.candidates = candidates


def validate_webhook(value):
    parsed = urlparse(value.strip())
    if (parsed.scheme != 'https' or parsed.netloc != 'oapi.dingtalk.com' or
            parsed.path != '/robot/send' or not parse_qs(parsed.query).get('access_token') or parsed.fragment):
        raise SetupError('请粘贴钉钉自定义机器人的完整 Webhook 地址，以 https://oapi.dingtalk.com/robot/send 开头。')


def has_delivery(root, allow_legacy=True):
    from notification_robots import RobotStore
    store = RobotStore(root)
    if allow_legacy:
        store.migrate_environment()
    return any('messages' in r['channels'] for r in store.list_robots())


class SetupService:
    def __init__(self, root, *, dws_path=None, portable=False):
        self.root = Path(root)
        self.dws_path = dws_path
        self.portable = portable

    def cli(self, args, profile=None, timeout=60):
        if not self.dws_path:
            from monitor_portable_install import bundled_dws
            self.dws_path = bundled_dws()
        command = [self.dws_path, *args, '--format', 'json']
        if profile:
            command += ['--profile', profile]
        try:
            result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SetupError('操作超时。请检查网络；登录时请完成浏览器中的钉钉授权。') from None
        if result.returncode:
            raise SetupError('钉钉操作未完成，请检查网络、账号权限，或重新登录授权。')
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise SetupError('钉钉没有返回可用结果，请重试。') from None
        if not isinstance(data, dict) or data.get('success') is False:
            raise SetupError('钉钉操作未成功，请重新登录后再试。')
        return data

    def profiles(self):
        data = self.cli(['profile', 'list'])
        profiles = []
        for p in data.get('profiles', []):
            if p.get('corpId') and p.get('userId'):
                profiles.append({'key': f"{p['corpId']}:{p['userId']}",
                    'label': f"{p.get('corpName', p['corpId'])} · {p.get('userName', p['userId'])}",
                    'current': p.get('isOrgCurrent') is True and p.get('isCurrent') is True})
        return profiles

    def login(self):
        self.cli(['auth', 'login', '--recommend'], timeout=300)
        return self.profiles()

    def check_account(self, profile):
        result = self.cli(['auth', 'status'], profile=profile)
        if not result.get('authenticated') or not result.get('token_valid'):
            raise SetupError('账号授权尚未完成或已失效，请点击“登录钉钉”。')

    def initial_rows(self):
        sources = read_json(self.root / 'monitor_sources.json', [])
        return [{key: source.get(key, False if key == 'AllowReopen' else '')
                 for key in ('GroupName', 'SenderName', 'Terms', 'AllowReopen')}
                for source in sources] if sources else [dict(r) for r in DEFAULT_ROWS]

    @staticmethod
    def choose_unique(title, candidates, choose):
        if not candidates:
            raise SetupError(title + '：未找到。请核对名称，并确认当前账号在该群内。')
        if len(candidates) == 1:
            return candidates[0]
        if choose is None:
            raise ChoiceRequired(title + '：有多个结果，请选择。', candidates)
        selected = choose(title, candidates)
        if selected not in candidates:
            raise SetupError('已取消选择，配置未保存。')
        return selected

    def resolve_sources(self, rows, profile, choose=None, progress=None):
        if not rows or not profile or ':' not in profile:
            raise SetupError('请选择钉钉账号，并至少保留一个监控对象。')
        output = []
        seen = set()
        existing = read_json(self.root / 'monitor_sources.json', [])
        for index, row in enumerate(rows):
            group_name, sender_name, terms = (str(row.get(k, '')).strip() for k in ('GroupName', 'SenderName', 'Terms'))
            if not group_name or not sender_name or not terms:
                raise SetupError(f'第 {index + 1} 行的群名、发送人和关键词都需要填写。')
            if progress:
                progress(f'正在核对 {index + 1}/{len(rows)}：{group_name} · {sender_name}')
            groups = self.cli(['chat', '+chat-search', '--query', group_name], profile=profile)
            if not groups.get('complete') or groups.get('partial'):
                raise SetupError('群搜索结果不完整，请重试后再保存。')
            matches = [g for g in groups.get('chats', []) if g.get('title', g.get('name')) == group_name
                       and g.get('openConversationId')]
            group = self.choose_unique(group_name, [dict(label=f"{group_name} · {g.get('memberCount', '?')}人 · {g['openConversationId'][-8:]}",
                id=g['openConversationId']) for g in matches], choose)
            members = self.cli(['chat', '+chat-members-list', '--group', group['id']], profile=profile)
            if not members.get('complete') or members.get('partial'):
                raise SetupError(f'{group_name} 的成员读取不完整，请重试。')
            people = [p for p in members.get('users', []) if sender_name in (p.get('name'), p.get('nick'))
                      and p.get('openDingtalkId')]
            person = self.choose_unique(sender_name, [dict(label=f"{p.get('name', sender_name)} · {p.get('role', '成员')} · {p['openDingtalkId'][-8:]}",
                id=p['openDingtalkId']) for p in people], choose)
            pair = (group['id'], person['id'])
            if pair in seen:
                raise SetupError(f'{group_name} · {sender_name} 重复了，请只保留一行。')
            seen.add(pair)
            state = 'source_' + hashlib.sha256((profile + '|'.join(pair)).encode()).hexdigest()[:20] + '.json'
            for old in existing:
                if (old.get('GroupId'), old.get('SenderId')) == pair and old.get('Profile', profile) == profile:
                    state = old['State']
            output.append(dict(GroupId=group['id'], SenderId=person['id'], GroupName=group_name,
                SenderName=sender_name, Terms=terms.replace('，', ','), AllowReopen=bool(row.get('AllowReopen')),
                State=state, Profile=profile))
        return output

    def save(self, sources, webhook, controller, enable=True):
        if not sources:
            raise SetupError('监控对象尚未核对，无法保存。')
        if webhook.strip():
            validate_webhook(webhook)
        elif not has_delivery(self.root, allow_legacy=not self.portable):
            raise SetupError('请先填写接收群的机器人地址。')
        controller.stop()
        deadline = time.monotonic() + 40
        while controller.status()['state'] not in ('paused', 'offline'):
            if time.monotonic() > deadline:
                raise SetupError('旧监听尚未结束。请稍候再保存；旧配置未更改。')
            time.sleep(.25)
        if any(s['live'] for s in controller.status()['sources']):
            raise SetupError('仍有旧监听在运行，请稍候再试。')
        self.root.mkdir(parents=True, exist_ok=True)
        if webhook.strip():
            from notification_robots import RobotStore
            store = RobotStore(self.root)
            old = next((r for r in store.list_robots() if r['id'] == 'legacy-messages'), None)
            store.save('货源消息机器人', webhook, ['messages'], robot_id=old['id'] if old else None)
        write_json(self.root / 'monitor_sources.json', sources)
        write_json(self.root / 'setup_complete.json', {'version': 2, 'configured_at': time.time(),
                   'source_count': len(sources)})
        if enable:
            controller.start()
