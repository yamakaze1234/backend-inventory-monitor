"""Private JSON-lines worker using the downloaded tool's isolated Python env."""
import contextlib
import asyncio
import importlib.util
import json
from pathlib import Path
import sys


def collect_all(client):
    """Fail closed on incomplete/repeated pagination; emit normalized stock only."""
    if not client.is_logged_in():
        raise ValueError('login_required')
    client._submit(client._refresh_auth(), timeout=40)
    result, seen, expected = [], set(), None
    for page in range(1, 201):
        raw = client._list_page_raw(page=page, page_size=100)
        body = raw.get('resp_body')
        if raw.get('status') != 200 or not isinstance(body, dict) or body.get('code') != 0:
            raise ValueError('query_failed')
        total = body.get('total')
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError('invalid_total')
        if expected is None:
            expected = total
        if expected != total or not isinstance(body.get('data'), list):
            raise ValueError('unstable_pagination')
        for product in body['data']:
            if not isinstance(product, dict) or not isinstance(product.get('skus'), list):
                raise ValueError('incomplete_skus')
            ident = product.get('product_id')
            if isinstance(ident, bool) or not isinstance(ident, (str, int)) or not str(ident):
                raise ValueError('invalid_product')
            ident = str(ident)
            if ident in seen:
                raise ValueError('repeated_page')
            seen.add(ident)
        result.extend(client._parse_response(body))
        if len(result) == expected:
            return result
        if len(result) > expected or not body['data']:
            raise ValueError('incomplete_pagination')
    raise ValueError('pagination_limit')


def main():
    tool_path, profile = map(Path, sys.argv[1:3])
    spec = importlib.util.spec_from_file_location('douyin_stock_client', tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if sys.argv[3:] == ['--probe-browser']:
        async def probe():
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=module._find_edge(), headless=True)
                await browser.close()
        asyncio.run(probe())
        print(json.dumps({'ok': True, 'browser': True}), flush=True)
        return
    client = module.DouYinStockClient(user_data_dir=profile)
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                with contextlib.redirect_stdout(sys.stderr):
                    if request['op'] == 'login':
                        result = {'logged_in': bool(client.start_browser())}
                    elif request['op'] == 'collect':
                        result = {'products': collect_all(client), 'complete': True}
                    elif request['op'] == 'close':
                        break
                    else:
                        raise ValueError('unknown_operation')
                response = {'ok': True, **result}
            except Exception:
                # Never return exception strings, request headers, cookies or raw bodies.
                response = {'ok': False, 'error': '查询未完成，请核验登录状态及库存页面。'}
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
