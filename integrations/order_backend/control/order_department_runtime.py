"""部门订单运行上下文：只读取公开路径和范围，不读取登录资料。"""
import json
import os
from pathlib import Path


def settings():
    path=os.environ.get('ORDER_BOT_CONFIG','')
    if not path:
        pointer=Path(__file__).with_name('order-department.json')
        if pointer.is_file():path=json.loads(pointer.read_text(encoding='utf-8'))['configPath']
    if not path:raise ValueError('尚未配置本部门订单匹配运行环境')
    path=Path(path).resolve()
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('version')!=1 or not value.get('shops'):raise ValueError('订单部门配置格式无效')
    os.environ['ORDER_BOT_CONFIG']=str(path)
    return value
