"""Fetch all configured source messages in an explicit same-day range."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from monitor_setup_service import SetupService
from inventory_monitor import InventoryService

def collect(root, now):
    root=Path(root)
    sources=json.loads((root/'monitor_sources.json').read_text('utf-8-sig'))
    if not sources: raise ValueError('未配置货源消息来源')
    local=datetime.fromtimestamp(now,timezone(timedelta(hours=8)))
    start=local.replace(hour=0,minute=0,second=0,microsecond=0)
    setup=SetupService(root)
    collected=[]
    for source in sources:
        if not source.get('Profile'): raise ValueError('货源来源缺少登录身份')
        result=setup.cli(['chat','+search-msg','--group',source['GroupId'],
                         '--sender',source['SenderId'],'--start',start.isoformat(),
                         '--end',local.isoformat(),'--page-all','--page-limit','40'],
                         profile=source['Profile'],timeout=180)
        if (result.get('complete') is not True or result.get('hasMore') or result.get('failures')
                or result.get('failedCount') or result.get('scope',{}).get('resultsWithinScope') is not True
                or result.get('senderScope',{}).get('resultsWithinScope') is not True):
            raise ValueError('当天货源记录未完整获取，停止日报发送')
        for message in result.get('messages',[]):
            if message.get('conversationId')!=source['GroupId'] or message.get('senderId')!=source['SenderId']:
                raise ValueError('货源记录来源不匹配')
            collected.append(dict(message_id=message['messageId'],text=message.get('text',''),
                                  created_at=message.get('createTime'),sender=source['SenderName'],
                                  group=source['GroupName'],group_id=source['GroupId'],sender_id=source['SenderId']))
    service=InventoryService(root)
    for message in collected: service.record_message(message)
    with service.db() as db:
        service.put(db,'history_coverage',dict(start=start.timestamp(),end=now,sources=len(sources),count=len(collected)))
    return collected
