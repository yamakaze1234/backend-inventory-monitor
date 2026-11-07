"""Strict, complete local handoff contract from the DIY workbench."""
import hashlib
import json
import re
from pathlib import Path

MAX_BYTES = 20_000_000
MAX_CONFIGS = 5000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def read_manifest(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('联动快照不存在或超过大小上限')
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('联动快照读取或解析失败') from None
    if not isinstance(manifest, dict):
        raise ValueError('联动快照格式无效')
    content_hash = manifest.get('contentHash')
    without_hash = {k: v for k, v in manifest.items() if k != 'contentHash'}
    if (manifest.get('contract') != 'profit-monitor/v1' or manifest.get('schemaVersion') != 1
            or manifest.get('complete') is not True or manifest.get('formulaVersion') != 'profit/v1'
            or not isinstance(content_hash, str) or sha(without_hash) != content_hash):
        raise ValueError('联动快照版本、完整标记或哈希无效')
    if not isinstance(manifest.get('sourceId'), str) or not manifest['sourceId'] or not isinstance(manifest.get('workspaceId'), str) or not manifest['workspaceId']:
        raise ValueError('联动来源身份缺失')
    if type(manifest.get('revision')) is not int or manifest['revision'] < 0 or not isinstance(manifest.get('publishedAt'), str):
        raise ValueError('联动修订或发布时间无效')
    configs = manifest.get('configs')
    if not isinstance(configs, list) or len(configs) > MAX_CONFIGS or sha(configs) != manifest.get('businessHash'):
        raise ValueError('联动配置清单不完整')
    seen = set()
    for config in configs:
        if not isinstance(config, dict):
            raise ValueError('联动配置无效')
        if any(field not in config for field in ('priceCents', 'feeCents', 'parts', 'product', 'name', 'installment')):
            raise ValueError('联动配置必需字段缺失')
        if any(not isinstance(config[field], str) or len(config[field]) > 300 for field in ('product', 'name')):
            raise ValueError('配置名称无效')
        key = (config.get('shopId'), config.get('configId'))
        if not all(isinstance(v, str) and v and len(v) <= 150 for v in key) or key in seen:
            raise ValueError('联动配置身份缺失或重复')
        seen.add(key)
        if type(config.get('priceCents')) is not int and config.get('priceCents') is not None:
            raise ValueError('到手价格式无效')
        if config.get('priceCents') is not None and config['priceCents'] < 0:
            raise ValueError('到手价格式无效')
        if config.get('installment') not in (0, 12, 24, '0', '12', '24'):
            raise ValueError('分期格式无效')
        if type(config.get('feeCents')) is not int and config.get('feeCents') is not None:
            raise ValueError('分期费格式无效')
        if config['feeCents'] is not None and config['feeCents'] < 0:
            raise ValueError('分期费格式无效')
        parts = config.get('parts')
        if not isinstance(parts, list) or len(parts) > 200:
            raise ValueError('实际配件清单无效')
        line_ids = set()
        for part in parts:
            if not isinstance(part, dict) or any(field not in part for field in ('lineId', 'slot', 'name', 'goodsId', 'qty', 'taxCents')):
                raise ValueError('实际配件字段缺失')
            if not isinstance(part.get('lineId'), str) or not part['lineId'] or len(part['lineId']) > 200 or part['lineId'] in line_ids:
                raise ValueError('实际配件行身份无效或重复')
            if any(not isinstance(part[field], str) or len(part[field]) > 300 for field in ('slot', 'name')):
                raise ValueError('实际配件名称无效')
            line_ids.add(part['lineId'])
            gid = part.get('goodsId')
            if not isinstance(gid, str) or len(gid) > 100:
                raise ValueError('ERP 商品 ID 格式无效')
            if part.get('qty') is not None and (type(part['qty']) is not int or not 1 <= part['qty'] <= 1000):
                raise ValueError('配件数量无效')
            tax = part.get('taxCents')
            if tax is not None and (type(tax) is not int or tax < 0):
                raise ValueError('含税核算成本无效')
    return manifest
