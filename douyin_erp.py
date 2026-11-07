"""将精确 SKU 套餐配件与公司大库可销库存关联；整套可组装量是单配置口径。"""
from collections import Counter
from pathlib import Path
import json
import math
import subprocess
import sys
import time
import os
import threading

_SPAWN_LOCK = threading.Lock()


def run_bridge(command, *, input, timeout, **kwargs):
    """外部匹配工具使用自己的 Python，避免继承打包程序的 DLL 搜索目录。"""
    env = os.environ.copy()
    bundle = str(getattr(sys, '_MEIPASS', ''))
    if bundle:
        env['PATH'] = os.pathsep.join(p for p in env.get('PATH', '').split(os.pathsep)
                                    if not os.path.abspath(p).casefold().startswith(bundle.casefold()))
    kwargs.pop('capture_output', None)
    with _SPAWN_LOCK:
        frozen_windows = sys.platform == 'win32' and bool(bundle)
        if frozen_windows:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, **kwargs)
        finally:
            if frozen_windows:
                ctypes.windll.kernel32.SetDllDirectoryW(bundle)
    with process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill(); process.communicate()
            raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

# 旧关联查询工具路径优先使用用户保存配置，公开版不携带开发机路径。
DEFAULT_MATCH_ROOT = Path(__file__).resolve().parent / 'matching-tool'


def assess(parts, stock):
    """重复配件先合并需求；缺失库存不当零，待入库存不计入可组装量。"""
    needs, shortages, unknown, counts = Counter(), [], [], []
    for gid, qty in parts.items():
        if type(qty) is not int or qty <= 0:
            unknown.append(f'{gid}：配件用量无效')
        else:
            needs[str(gid)] += qty
    if not needs:
        unknown.append('未取得有效配件清单')
    for gid, need in needs.items():
        item = stock.get(gid, {})
        available = item.get('able')
        name = item.get('name') or gid
        if type(available) not in (int, float) or not math.isfinite(available):
            unknown.append(f'{name}：' + (item.get('unknown_reason') or '公司大库库存未知'))
            continue
        counts.append(max(0, math.floor(available / need)))
        if available < need:
            shortages.append(dict(goods_id=gid, name=name, required=need, available=available,
                                  shortage=need-max(0, available), incoming=item.get('purchase')))
    return dict(state='缺货' if shortages else ('待核对' if unknown else '配件齐全'),
                buildable=None if unknown or not counts else min(counts),
                shortages=shortages, unknown=unknown, checked=time.time(), warehouse='公司大库')


def attach_erp(service, shop, products, *, root=None, runner=None):
    """使用现有匹配工具和已连接 SQL 源；不加载、复制或打印认证配置。"""
    if root is None:
        with service.db() as db:
            root = service.get(db, 'douyin_match_root', str(DEFAULT_MATCH_ROOT))
    root = Path(root)
    python = root / '.runtime-tiny-tools-016/Scripts/python.exe'
    script = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'douyin_erp_bridge.py'
    source = getattr(getattr(service, 'sql_worker', None), 'source', None)
    if not python.is_file() or not (root / 'app/src/batch_match.py').is_file():
        reason = '未找到本机匹配工具；请设置 ERP 关联目录'
        mapped = None
    elif source is None:
        reason, mapped = 'ERP 库存连接未就绪', None
    else:
        try:
            # -I 不把打包目录加入 sys.path；3.14 匹配运行时不能加载主程序的 3.13 扩展。
            result = (runner or run_bridge)([str(python), '-I', '-B', '-X', 'utf8', str(script)],
                input=json.dumps(dict(root=str(root), shop=shop, products=products), ensure_ascii=False),
                capture_output=True, text=True, encoding='utf-8', timeout=1200,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            mapped = json.loads(result.stdout)
            if result.returncode or mapped.get('ok') is not True:
                raise ValueError('ERP 关联查询失败')
        except Exception:
            reason, mapped = 'ERP 关联查询失败；请核对匹配工具登录', None
    stock = {}
    if mapped:
        from inventory_health import normalize_isolated
        gids = sorted({g for row in mapped['rows'].values() if row.get('state') == 'ready' for g in row['parts']})
        for start in range(0, len(gids), 500):
            try:
                issues = {}
                raw = source.read_bundle(gids[start:start+500])
                catalog, _ = normalize_isolated(raw, legacy_text=source.legacy_text, issues=issues)
                stock.update({r['goods_id']: r for r in catalog if r['goods_id'] not in issues})
            except Exception:
                # 单批失败标未知，其余成功批次继续；不使用过期缓存填补。
                continue
        with service.db() as db:
            if service.get(db, 'scope') != source.scope:
                # 用户在巡检期间切换 ERP 来源时，不能混用旧连接的库存。
                stock = {}
    return enrich_products(products, mapped, stock, reason if not mapped else '')


def enrich_products(products, mapped, stock, reason='ERP 未核对'):
    """纯数据合并供运行时与已验证查询快照复核共用，输出相同的处理建议。"""
    for product in products:
        for sku in product['skus']:
            key = product['product_id'] + ':' + sku['sku_id']
            bom = mapped['rows'].get(key, {}) if mapped else {}
            if bom.get('state') == 'ready':
                erp = assess(bom['parts'], stock)
                erp['goods_id'] = bom['goods_id']
                erp['bom_observed'] = bom['observed']
            else:
                erp = dict(state='待核对', buildable=None, shortages=[],
                           unknown=[bom.get('state') or reason], checked=time.time())
            # 平台采集和 ERP 分别说明结果；下架、活动占用不强推“补平台库存”。
            from_export = sku.get('source') == 'export'
            q = sku.get('export_sellable') if from_export else sku.get('dom_stock')
            erp['action'] = ('核对售卖状态后补库存' if from_export and q == 0 and erp['state'] == '配件齐全' else
                             '可补平台库存' if q == 0 and erp['state'] == '配件齐全' else
                             '等待配件补货' if q == 0 and erp['state'] == '缺货' else
                             '检查履约风险' if q is not None and q > 0 and erp['state'] == '缺货' else
                             '待核对' if erp['state'] == '待核对' else '记录')
            sku['erp'] = erp
    return products


def attach_maintenance(service, config, products):
    """维护表提供配置，ERP SQL 批量提供实时库存；不读取网店关联或 ERP 套餐。"""
    from douyin_maintenance import read_maintenance, KNOWN_SHOPS
    started = time.monotonic()
    try:
        if not config.get('maintenance_path'):
            raise ValueError('未设置主机维护表，请点击“选择文件”配置本店文件')
        expected = KNOWN_SHOPS.get(config['name'])
        if expected and expected != config.get('maintenance_shop_id'):
            raise ValueError('维护表店铺编号与当前店铺不符')
        data = read_maintenance(config['maintenance_path'], config['maintenance_sheet'], config['maintenance_shop_id'])
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else '维护表无法读取，请检查路径或文件是否已保存'
        for product in products:
            for sku in product['skus']:
                sku['erp'] = dict(source='maintenance', state='待核对', parts=[], shortages=[], unknown=[reason], checked=time.time())
        return products
    wanted = {p['product_id'] + ':' + s['sku_id'] for p in products for s in p['skus']}
    matched = {key: data['rows'][key] for key in wanted if key in data['rows']}
    gids = sorted({gid for row in matched.values() if row['state'] == 'ready' for gid in row['parts']})
    source = getattr(getattr(service, 'sql_worker', None), 'source', None)
    stock = {}
    stock_errors = {}
    if source is not None:
        from inventory_health import normalize_isolated
        for start in range(0, len(gids), 500):
            try:
                issues = {}
                catalog, details = normalize_isolated(source.read_bundle(gids[start:start + 500]),
                                                legacy_text=source.legacy_text, issues=issues)
                stock_errors.update({gid: 'ERP 数据已隔离：' + item['reason'] for gid, item in issues.items()})
                for item in catalog:
                    if item['goods_id'] in issues:
                        continue
                    if item.get('able') is None:
                        depots = details.get(item['sku'], {}).get('depots', [])
                        main = next((d for d in depots if d['name'] == '公司大库'), None)
                        item['unknown_reason'] = ('公司大库可销数为空，未当作零库存' if main else
                            'ERP 未返回公司大库记录；其他库：' + ('、'.join(d['name'] for d in depots) or '无分库记录'))
                    stock[item['goods_id']] = item
            except Exception:
                stock_errors.update({gid: 'ERP 本批查询失败，未使用旧库存' for gid in gids[start:start + 500]})
        with service.db() as db:
            if service.get(db, 'scope') != source.scope:
                stock = {}
    # 未返回的编号与连接/数据错误分别标注，不以全库总数替代公司大库。
    for gid in gids:
        if gid not in stock:
            stock[gid] = dict(unknown_reason=('ERP 库存连接未就绪' if source is None else stock_errors.get(gid, 'ERP 未返回该配件编号')))
    known_products = {key.split(':')[0] for key in data['rows']}
    for product in products:
        for sku in product['skus']:
            row = matched.get(product['product_id'] + ':' + sku['sku_id'])
            if row and row['state'] == 'ready':
                # 缺货结论与配件明细共用同一份需求，重复配件已在解析时累计用量。
                erp = assess(row['parts'], stock)
                erp['parts'] = [dict(goods_id=gid, name=stock.get(gid, {}).get('name') or row['names'].get(gid) or gid,
                                     required=n, available=stock.get(gid, {}).get('able')) for gid, n in row['parts'].items()]
                if source is None:
                    erp['unknown'] = ['ERP 库存连接未就绪']
            else:
                erp = dict(state='待核对', parts=[], shortages=[], checked=time.time(),
                           unknown=[row['state'] if row else ('维护表已有商品 ID，但未找到该 SKU ID' if product['product_id'] in known_products else '维护表未找到该商品 ID')])
            erp['matching_state'] = 'matched' if row else 'sku_unmatched'
            erp.update(source='maintenance', workbook=data['source'], sheet=data['sheet'], row=row.get('row') if row else None,
                       workbook_modified=data['modified'], source_hash=data['sha256'], elapsed_seconds=round(time.monotonic()-started, 3))
            erp['action'] = '等待配件补货' if erp['state'] == '缺货' else '待核对' if erp['state'] == '待核对' else '按维护表配件齐全'
            sku['erp'] = erp
    return products
