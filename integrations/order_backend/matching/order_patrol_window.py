"""订单自动巡检的北京时间窗口；不控制手动任务或发货监控。"""
from datetime import datetime, timedelta, timezone
import json
import re

SETTING = 'matchPatrolWindow'
DEFAULT = ('18:30', '00:00')
CN = timezone(timedelta(hours=8))


def validate(start, end):
    if any(not isinstance(t,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',t) for t in (start,end)):
        raise ValueError('请选择有效的开始和结束时间（北京时间）')
    if start == end:
        raise ValueError('开始和结束时间不能相同')
    return start,end


def read(raw):
    if raw is None:
        return DEFAULT
    try:
        data = json.loads(raw)
        return validate(data['start'],data['end'])
    except (TypeError,ValueError,KeyError):
        # 已损坏的设置不能退回全天运行；界面允许重新保存。
        return None


def slot_start(window, at):
    """返回当前允许开始的时段起点；跨午夜时归到开始那一日。"""
    if not window:
        return None
    start,end = window
    current = datetime.fromtimestamp(at,CN)
    now = current.strftime('%H:%M')
    if not (start<=now<end if start<end else now>=start or now<end):
        return None
    day = current.date() - timedelta(days=int(start>end and now<end))
    hour,minute = map(int,start.split(':'))
    return datetime(day.year,day.month,day.day,hour,minute,tzinfo=CN).timestamp()


def due(window, at, previous, interval_minutes):
    start = slot_start(window,at)
    # 每天开窗后允许首轮，即使上一轮的间隔跨到了第二天。
    return start is not None and (previous<start or at-previous>=interval_minutes*60)


def describe(raw, at):
    window = read(raw)
    start,end = window or DEFAULT
    return dict(start=start,end=end,nextDay=end<start,valid=window is not None,
                inWindow=slot_start(window,at) is not None)
