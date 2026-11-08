"""机器人查询 CLI：仅调用本机只读接口，令牌从环境变量读取，不放命令参数。"""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    parser=argparse.ArgumentParser(description='后台库存监控只读查询')
    parser.add_argument('--url',required=True)
    parser.add_argument('--token-env',default='INVENTORY_QUERY_TOKEN')
    parser.add_argument('--request',type=Path,help='JSON 查询文件；省略则读取数据集目录')
    parser.add_argument('--refresh',action='store_true',help='显式更新价格变化（只读 ERP，不发送提醒）')
    parser.add_argument('--job',help='查看价格查询任务状态')
    args=parser.parse_args()
    parsed=urlsplit(args.url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('仅允许 http://127.0.0.1:端口')
    token=os.environ.get(args.token_env)
    if not token:raise ValueError('未配置只读查询令牌环境变量')
    payload=None
    if args.request:
        if args.request.stat().st_size>32768:raise ValueError('查询文件过大')
        value=json.loads(args.request.read_text(encoding='utf-8-sig'))
        if not isinstance(value,dict):raise ValueError('查询必须是 JSON 对象')
        payload=json.dumps(value).encode()
    if args.refresh and args.job or args.job and payload:raise ValueError('任务与查询参数不能混用')
    if args.refresh:
        import uuid
        payload=json.dumps(dict(dataset='price_changes',request_id='price-'+uuid.uuid4().hex)).encode()
    from urllib.parse import quote
    route='/api/data/job?id='+quote(args.job,safe='') if args.job else '/api/data/refresh' if args.refresh else '/api/data/query' if payload else '/api/data/catalog'
    request=Request(args.url.rstrip('/')+route,data=payload,
        headers={'X-Console-Token':token,'Content-Type':'application/json'})
    opener=build_opener(ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=30) as response:result=json.load(response)
    except HTTPError as error:
        try:result=json.load(error)
        except (ValueError,TypeError):result={'code':'HTTP_ERROR','error':'查询失败'}
        print(json.dumps(result,ensure_ascii=False));return 1
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        print(json.dumps({'code':'CLIENT_ERROR','error':'查询未完成，请检查本机地址、令牌环境变量和请求文件'},ensure_ascii=False))
        sys.exit(1)
