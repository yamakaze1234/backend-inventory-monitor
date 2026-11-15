"""部署仅替换本软件管理的 MCP 注册，保留同工作区其他工具。"""
import re
import tomllib


def merge(existing, replacement):
    # 先校验 TOML；损坏配置不能通过更新被悄悄抹除。
    tomllib.loads(existing)
    managed={'erp_order_matching','erp_daily_report'}
    kept=[]
    skip=False
    for line in existing.splitlines(keepends=True):
        if line.lstrip().startswith('['):
            match=re.match(r'\s*\[mcp_servers\.([\w-]+)(?:\.[\w-]+)?\]\s*$',line.strip())
            skip=bool(match and match.group(1) in managed)
        if not skip:kept.append(line)
    result=''.join(kept).rstrip()+'\n'+replacement
    tomllib.loads(result)
    return result
