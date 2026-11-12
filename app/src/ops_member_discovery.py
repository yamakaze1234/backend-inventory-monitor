"""从已绑定群查真人，并按钉钉的开放身份与组织账号交叉核实。"""
from collections import Counter
from hashlib import sha256
import unicodedata

from notification_enterprise import EnterpriseClient
from ops_command_api import checked_id
from ops_group_discovery import preferred, profile_org


def display_name(value):
    # 群昵称用于展示和搜索，不能作为授权键。
    return ' '.join(value.split())[:80] if isinstance(value, str) else ''


class MemberDirectory(EnterpriseClient):
    def roster(self, profile, group):
        response = self.call(['chat', '+chat-members-list', '--conversation-id', group, '--member-types', 'user'], profile)
        self.complete(response)
        if response.get('conversationId') != group or not isinstance(response.get('users'), list):
            raise ValueError('钉钉未返回所选群的成员，请重试')
        members = {}
        for row in response['users']:
            if not isinstance(row, dict):
                continue
            open_id = row.get('openDingtalkId')
            name, nick = (display_name(row.get(k)) for k in ('name', 'nick'))
            if not checked_id(open_id) or not name:
                continue
            label = f'{name}（{nick}）' if nick and nick != name else name
            members[open_id] = dict(open_id=open_id, name=name, nick=nick, label=label)
        counts = Counter(row['label'] for row in members.values())
        for row in members.values():
            if counts[row['label']] > 1:
                # 同名同昵称时补稳定识别码；不会默认选中其中任何人。
                suffix = sha256(row['open_id'].encode()).hexdigest()[:8]
                row['label'] += f' · {suffix}'
        return dict(sorted(((r['label'], r) for r in members.values()), key=lambda item:item[0].casefold()))

    def resolve(self, profile, member):
        response = self.call(['contact', 'user', 'search', '--query', member['name']], profile)
        rows = response.get('result')
        if not isinstance(rows, list) or response.get('hasMore'):
            raise ValueError('通讯录未完整返回，请重新搜索')
        # DWS 的群成员只返回开放 ID；仅接受同一 profile 下开放 ID 精确对应的组织成员。
        users = {row['userId'] for row in rows if isinstance(row, dict)
                 and row.get('openDingTalkId') == member['open_id'] and checked_id(row.get('userId'))}
        if len(users) != 1:
            raise ValueError(f"「{member['label']}」无法核实为当前组织的成员，未授予权限")
        return users.pop()

    def existing_names(self, profile, members):
        result = {}
        ids = [user for user in members if ',' not in user]
        for start in range(0, len(ids), 30):
            response = self.call(['contact', 'user', 'get', '--ids', ','.join(ids[start:start + 30])], profile)
            for row in response.get('result', []):
                if not isinstance(row, dict):
                    continue
                person = row.get('orgEmployeeModel', row)
                if not isinstance(person, dict):
                    continue
                user = person.get('orgUserId') or person.get('userId')
                name = display_name(person.get('orgUserName') or person.get('name'))
                if user in members and name:
                    result[user] = name
        return result


def context(b, identity, client=None):
    b.external()
    client = client or MemberDirectory()
    org = identity[0]
    _, _, pinned = preferred(b)
    profiles = [row for row in client.profiles() if profile_org(row['profile']) == org]
    if pinned and profile_org(pinned) == org:
        if pinned not in {r['profile'] for r in profiles}:
            raise ValueError('运营机器人的钉钉账号未登录')
        profile = pinned
    else:
        defaults = [r['profile'] for r in profiles if r.get('current')]
        if len(defaults) != 1:
            raise ValueError('请在钉钉连接设置中确认该组织的当前账号')
        profile = defaults[0]
    return client, profile


def search(roster, query):
    query = unicodedata.normalize('NFKC', query).casefold().split()
    return [row for label, row in roster.items()
            if all(term in unicodedata.normalize('NFKC', label).casefold() for term in query)]
