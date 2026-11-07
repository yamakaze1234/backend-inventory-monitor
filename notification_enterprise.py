"""企业机器人通过本机 DWS 发送；只保存账号和群标识，不接触登录凭据。"""
from pathlib import Path
import json
import subprocess
import sys


def validate_target(target):
    """拒绝空目标与非企业机器人，避免错误身份或错误组织的隐式回退。"""
    keys = ('profile', 'group_id', 'group_name', 'robot_code', 'robot_name')
    if not isinstance(target, dict) or any(not isinstance(target.get(k), str) or not target[k].strip() for k in keys):
        raise ValueError('请选择钉钉账号、目标群和群内的企业机器人。')
    if not target['group_id'].startswith('cid') or not target['robot_code'].startswith('ding'):
        raise ValueError('请选择群内的企业机器人，普通 Webhook 机器人请使用 Webhook 模式。')
    return {k: target[k].strip() for k in keys}


class EnterpriseClient:
    def call(self, args, profile=None):
        # 打包版自带 DWS，开发版复用本机安装；不拼接 shell 命令。
        executable = Path(getattr(sys, '_MEIPASS', Path.home() / '.local/bin')) / 'dws.exe'
        if not executable.is_file():
            raise ValueError('未找到 DWS，请检查程序安装。')
        command = [str(executable), *args, '--format', 'json']
        if profile:
            command.extend(['--profile', profile])
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # 原始错误可能包含认证内容，界面只显示安全提示。
        if result.returncode:
            raise ValueError('钉钉操作失败，请检查登录状态、群权限和机器人权限。')
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise ValueError('钉钉返回结果无法识别，请稍后刷新。') from None
        if not isinstance(value, dict) or value.get('success') is False or value.get('ok') is False or value.get('error'):
            raise ValueError('钉钉操作未成功，请检查登录状态和权限。')
        return value

    def profiles(self):
        return [dict(profile=p['profile'], label=f"{p.get('corpName', '')} · {p.get('userName', '')}",
                     current=p.get('isOrgCurrent') is True)
                for p in self.call(['profile', 'list']).get('profiles', []) if p.get('profile')]

    def groups(self, profile, query):
        value = self.call(['chat', '+chat-search', '--query', query], profile)
        self.complete(value)
        return [dict(group_id=g['openConversationId'], group_name=g.get('name') or g.get('title'))
                for g in value.get('chats', []) if g.get('openConversationId') and (g.get('name') or g.get('title'))]

    @staticmethod
    def complete(value):
        if value.get('complete') is not True or value.get('hasMore') or value.get('partial') or value.get('failures'):
            raise ValueError('钉钉查询未完整返回，请缩小群名范围或稍后重试。')

    def bots(self, profile, group_id):
        value = self.call(['chat', '+chat-members-list', '--group', group_id], profile)
        self.complete(value)
        if value.get('conversationId') != group_id:
            raise ValueError('返回的群与选择不一致，请重新选择。')
        return [dict(robot_code=b['robotCode'], robot_name=b['name']) for b in value.get('bots', [])
                if str(b.get('robotCode', '')).startswith('ding') and b.get('name')]

    def verify(self, target):
        target = validate_target(target)
        if not any(b['robot_code'] == target['robot_code'] for b in self.bots(target['profile'], target['group_id'])):
            raise ValueError('该企业机器人已不在所选群中，请刷新后重选。')
        return target


def send_enterprise(target, payload):
    """调用发送接口前校验群成员；超时或未知结果不自动重发。"""
    client = EnterpriseClient()
    try:
        target = client.verify(target)
        if payload.get('msgtype') != 'markdown' or not payload.get('markdown', {}).get('text'):
            return dict(status='failed', retryable=False)
    except Exception:
        return dict(status='failed', retryable=False)
    try:
        value = client.call(['chat', '+messages-send', '--as', 'bot', '--robot-code', target['robot_code'],
                             '--group', target['group_id'], '--title', payload['markdown'].get('title') or '通知',
                             '--markdown', payload['markdown']['text'], '--yes'], target['profile'])
        result = value.get('result', {})
        accepted = value.get('ok') is True and result.get('success') is True and result.get('result', {}).get('processQueryKey')
        return dict(status='sent' if accepted else 'unknown', retryable=False)
    except Exception:
        # 发送已开始，任何异常都保留未知状态，不能退回个人身份或重复发送。
        return dict(status='unknown', retryable=False)
