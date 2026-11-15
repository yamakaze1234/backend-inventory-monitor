"""桌面复用随兼容后端部署的时段规则，避免两个进程的边界不同。"""
from functools import lru_cache
import runpy
from order_bot_config import bundle_root


@lru_cache(maxsize=1)
def tools():
    return runpy.run_path(str(bundle_root() / 'matching/patrol_time_window.py'))


def allowed(root, key, at=None):
    return tools()['allowed'](root, key, at)


def load(root, key):
    return tools()['load'](root, key)


def save(root, key, all_day, start=None, end=None):
    return tools()['save'](root, key, all_day, start, end)
