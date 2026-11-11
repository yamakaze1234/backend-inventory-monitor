"""北京时间每日固定巡检时刻；重启后沿用已保存的下一次时间。"""
from datetime import datetime, timedelta, timezone
import re

TZ = timezone(timedelta(hours=8))


def normalize_times(value):
    """允许一个或多个时间点，默认界面提供每天一次。"""
    values = value.replace('，', ',').split(',') if isinstance(value, str) else value
    if not isinstance(values, list) or not 1 <= len(values) <= 24:
        raise ValueError('请填写 1 至 24 个北京时间，例如 09:00')
    result = []
    for item in values:
        item = str(item).strip()
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', item):
            raise ValueError('巡检时间必须为 HH:MM，例如 09:00')
        result.append(item)
    return sorted(set(result))


def next_daily(now, times):
    """只返回未来时刻；错过多天时只补查一次，不连续补发历史批次。"""
    current = datetime.fromtimestamp(now, TZ)
    candidates = []
    for value in normalize_times(times):
        hour, minute = map(int, value.split(':'))
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        candidates.append(candidate.timestamp())
    return min(candidates)
