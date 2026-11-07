"""子进程复用现有匹配工具会话，只查网店关联和套餐配件，不执行匹配或刷新。"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import re
import sys
import time


def read_boms(request):
    root = Path(request['root']).resolve()
    sys.path.insert(0, str(root / 'app' / 'src'))
    from batch_match import gateway_session, load_profiles, SHOPS
    # 店名必须唯一命中匹配工具已配置的店铺，不能套用其他店铺的 ERP 编号。
    profiles = load_profiles()
    SHOPS.clear()
    SHOPS.update(profiles)
    shops = [str(k) for k, v in profiles.items() if v['name'] == request['shop']]
    if len(shops) != 1:
        raise ValueError('匹配工具中未找到唯一同名店铺')
    shop = shops[0]
    wanted = {(str(p['product_id']), str(s['sku_id'])) for p in request['products'] for s in p['skus']}
    output, selected = {}, {}
    with gateway_session(shop) as gateway:
        rows = gateway.client.net_goods(shop, '', 'SPU')
        keys = [(str(r.get('product_id')), str(r.get('sku_id'))) for r in rows]
        counts = Counter(keys)
        for row, key in zip(rows, keys):
            if key not in wanted:
                continue
            if str(row.get('shop_id')) != shop or counts[key] != 1:
                output[':'.join(key)] = {'state': '关联重复或店铺不符'}
                continue
            gid = str(row.get('goods_id') or '')
            kind = row.get('goods_c_bom_type')
            if not re.fullmatch(r'[1-9][0-9]*', gid) or kind not in ('group', 'part'):
                output[':'.join(key)] = {'state': 'ERP 未关联或套餐类型未知'}
                continue
            selected[key] = (gid, kind)
        def read_one(item):
            gid, kind = item
            try:
                parts = gateway.components(gid, fresh=True) if kind == 'group' else {gid: 1}
                if not parts:
                    raise ValueError('套餐无配件')
                return gid, dict(state='ready', parts=parts, observed=time.time(), goods_id=gid)
            except Exception:
                return gid, dict(state='套餐配件读取失败')
        # 限制并发；每轮读取真实关联和 BOM，不沿用可能已重匹配的历史套餐。
        with ThreadPoolExecutor(max_workers=4) as pool:
            boms = dict(pool.map(read_one, sorted(set(selected.values()))))
        for key, (gid, _) in selected.items():
            output[':'.join(key)] = boms[gid]
    for key in wanted:
        output.setdefault(':'.join(key), {'state': 'ERP 无精确关联'})
    return dict(ok=True, shop_id=shop, rows=output)


if __name__ == '__main__':
    try:
        result = read_boms(json.load(sys.stdin))
    except Exception:
        # 认证由原匹配工具管理；仅返回固定提示，绝不输出会话和底层异常。
        result = dict(ok=False, error='ERP 关联查询失败，请检查原匹配工具登录和店铺设置')
    print(json.dumps(result, ensure_ascii=False))
