"""为独立 Harness 注册工作区工具；不重写用户模型或钉钉账号配置。"""
import json
from pathlib import Path


def updated(config, workspace, existing):
    rows=[]
    for name,script in [('erp_order_matching','order-match-server.mjs'),('erp_daily_report','report-server.mjs')]:
        # 已有注册不猜测、不替换；必须明确指向当前控制目录。
        if name in existing:
            normalized=existing.replace('\\\\','\\').replace('/', '\\')
            if str(Path(config['control_root'])/script).replace('/', '\\') not in normalized:
                raise ValueError('Harness 已有其他路径的工具注册，请核对后重新配置')
            continue
        rows.append(dict(id='stock-monitor-'+name,name='@deepseek-ai/dsh-mcp-client',config=dict(
            transport='stdio',serverName=name,command=config['node_path'],
            args=[str(Path(config['control_root'])/script)],cwd=config['control_root'],
            toolCallTimeoutMs=300000,env={'ORDER_BOT_CONFIG':str(Path(workspace).resolve()/'order-bot.json')})))
    # JSON 流式映射也是合法 YAML；追加 list 元素保留已有插件及凭据字节。
    return existing+('' if existing.endswith('\n') or not existing else '\n')+(''.join('- '+json.dumps({'insert':rows},ensure_ascii=False)+'\n') if rows else '')
