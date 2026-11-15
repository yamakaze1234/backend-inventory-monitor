"""静态检查代码语法及资源路径，不连接业务系统、不生成缓存。"""
import ast
from pathlib import Path
root = Path(__file__).resolve().parents[1]
files = [p for folder in ('app/src', 'tests', 'tools') for p in (root/folder).rglob('*.py')]
for path in files:
    ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
for name in ('app/ui/monitor_assets/monitor.ico','app/ui/inventory-erp.user.js','docs/inventory-setup.md','tools/packaging/inventory_monitor.spec'):
    assert (root/name).is_file(), name
print(f'语法与资源路径检查通过：{len(files)} 个 Python 文件')
