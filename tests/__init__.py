"""库存监控回归测试的源码路径入口。"""
from pathlib import Path
import sys
_root = Path(__file__).resolve().parents[1]
for _path in (_root/'app/src', _root/'tests', _root/'.codex_tmp/tray_deps', _root/'beta/sql_inventory_20260917/deps'):
    sys.path.insert(0, str(_path))
