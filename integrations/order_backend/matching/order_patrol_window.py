"""订单自动巡检的北京时间窗口；不控制手动任务或发货监控。"""
from datetime import datetime, timedelta, timezone
import json
import re

SETTING = 'matchPatrolWindow'
DEFAULT = ('18:30', '00:00')
DEFAULT_INTERVAL = 120
CN = timezone(timedelta(hours=8))


def validate(start, end):
    if any(not isinstance(t,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',t) for t in (start,end)):
        raise ValueError('请选择有效的开始和结束时间（北京时间）')
    if start == end:
        raise ValueError('开始和结束时间不能相同')
    return start,end


def validate_interval(value):
    # 表单数字以字符串提交；只接受整数分钟，避免截断小数或布尔值。
    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,4}',value):
        value = int(value)
    if type(value) is not int or not 15<=value<=1440:
        raise ValueError('巡检间隔须为 15 至 1440 分钟的整数')
    return value


def read_settings(raw):
    if raw is None:
        return dict(start=DEFAULT[0],end=DEFAULT[1])
    try:
        data = json.loads(raw)
        start,end = validate(data['start'],data['end'])
        result = dict(start=start,end=end)
        if 'intervalMinutes' in data:
            result['intervalMinutes'] = validate_interval(data['intervalMinutes'])
        return result
    except (TypeError,ValueError,KeyError):
        # 已损坏的设置不能退回全天运行；界面允许重新保存。
        return None


def times(settings):
    return (settings['start'],settings['end']) if settings else None


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


def describe(raw, at, legacy_interval=DEFAULT_INTERVAL):
    settings = read_settings(raw)
    window = times(settings)
    start,end = window or DEFAULT
    # 旧队列尚未保存独立间隔时，沿用已绑定机器人的间隔。
    interval = (settings or {}).get('intervalMinutes',legacy_interval)
    return dict(start=start,end=end,nextDay=end<start,valid=window is not None,
                inWindow=slot_start(window,at) is not None,intervalMinutes=interval)
