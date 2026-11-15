"""将已保存的主机维护表转换为价格监控清单，严格沿用配件 ID 和配置身份。"""
from datetime import datetime, timezone
from pathlib import Path
import os
import re

from douyin_maintenance import digest, headers, platform_id, read_maintenance, text, workbook
from profit_manifest import MAX_BYTES, MAX_CONFIGS, canonical, sha

SOURCE_ID = 'maintenance-workbooks/v1'
VALID_GOODS_ID = re.compile(r'[1-9][0-9]{0,19}\Z')


def saved_sources(store):
    """只提取已保存的维护表绑定，不返回店铺登录或通知配置。"""
    return [dict(path=row['maintenance_path'], sheet=row['maintenance_sheet'],
                 shop_id=str(row['maintenance_shop_id']))
            for row in store.stores()
            if row.get('maintenance_path') and row.get('maintenance_sheet') and row.get('maintenance_shop_id')]


def normalize_sources(bindings):
    """固定顺序和来源身份；同一工作表不能重复加入巡检。"""
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 30:
        raise ValueError('请选择 1 至 30 个主机维护表工作表')
    result, seen = [], set()
    for item in bindings:
        if not isinstance(item, dict):
            raise ValueError('维护表绑定格式无效')
        path, sheet, shop = (item.get(key) for key in ('path', 'sheet', 'shop_id'))
        if not all(isinstance(value, str) and value.strip() for value in (path, sheet, shop)):
            raise ValueError('每个维护表均需填写文件、工作表和店铺编号')
        path = str(Path(path.strip()).resolve()); sheet = sheet.strip(); shop = shop.strip()
        if not VALID_GOODS_ID.fullmatch(shop):
            raise ValueError('维护表店铺编号需为正整数')
        key = (os.path.normcase(path), sheet)
        if key in seen:
            raise ValueError('同一维护表工作表不能重复绑定')
        seen.add(key)
        result.append(dict(path=path, sheet=sheet, shop_id=shop))
    return sorted(result, key=lambda row: (os.path.normcase(row['path']), row['sheet'], row['shop_id']))


def _read_sources(bindings):
    """逐来源校验后合并；异常行保留在预览中并排除，只监控有效配置中的配件。"""
    sources = normalize_sources(bindings)
    configs, issues, summaries, seen = [], [], [], set()
    for source in sources:
        path, sheet, shop = source['path'], source['sheet'], source['shop_id']
        data = read_maintenance(path, sheet, shop)
        slots_by_row = {}; headings = 0; without_spu = 0
        # 原库存读取器会忽略无平台身份行；价格预览额外报告身份损坏，避免静默少监控。
        with workbook(path) as (_, rows):
            header_row, columns = headers(rows, sheet)
            for number, values in rows(sheet):
                if number <= header_row:
                    continue
                get = lambda name: values.get(columns.get(name, -1))
                raw_spu, raw_sku = get('SPU'), get('SKU')
                part_content = bool(text(get('简称')) or any(
                    text(get(label)) or text(get(label[:-3])) for label in columns if label.endswith('_id')))
                content = part_content or bool(text(raw_sku))
                if not text(raw_spu):
                    # 没有 SPU 的未上架配置不纳入监控，但在来源预览中统计排除数量。
                    without_spu += int(content)
                    continue
                if not content or (not part_content and isinstance(raw_spu, str)
                                   and re.fullmatch(r'.*[^0-9][0-9]{15,20}', raw_spu.strip())
                                   and text(raw_sku) in ('活动直降', '下架')):
                    # 标题没有配置名和配件；只允许已核实的标题注记，不能吞掉损坏配置。
                    headings += 1
                    continue
                spu, sku = platform_id(raw_spu), platform_id(raw_sku)
                if not spu or not sku:
                    issues.append(dict(**source, row=number, reason='SPU／SKU 缺失或不是完整文本编号'))
                    continue
                slots = {}
                for label in columns:
                    if label.endswith('_id'):
                        gid = text(get(label))
                        if VALID_GOODS_ID.fullmatch(gid):
                            slots.setdefault(gid, []).append(label[:-3])
                slots_by_row[spu + ':' + sku] = slots
        if digest(path) != data['sha256']:
            raise ValueError('读取期间维护表已更新，请保存完成后重试')
        source_configs = 0
        for identity, item in sorted(data['rows'].items()):
            reason = ''
            if item['state'] != 'ready':
                reason = item['state']
            elif (shop, identity) in seen:
                reason = '多个维护表包含相同店铺／SPU／SKU，不能重复计入'
            elif any(not VALID_GOODS_ID.fullmatch(gid) or type(qty) is not int or not 1 <= qty <= 1000
                     for gid, qty in item['parts'].items()):
                reason = '配件编号或合并数量超出价格监控有效范围'
            if reason:
                issues.append(dict(**source, row=item['row'], reason=reason))
                continue
            spu, sku = identity.split(':', 1)
            slots = slots_by_row.get(identity, {})
            parts = [dict(lineId=gid, slot='／'.join(slots.get(gid, ['固定附加配件'])),
                          name=item['names'].get(gid, ''), goodsId=gid, qty=qty, taxCents=None)
                     for gid, qty in sorted(item['parts'].items())]
            if len(parts) > 200 or len(item['name']) > 300 or any(len(p['name']) > 300 or len(p['slot']) > 300 for p in parts):
                issues.append(dict(**source, row=item['row'], reason='配置名称或配件清单超出价格监控有效范围'))
                continue
            seen.add((shop, identity))
            configs.append(dict(shopId=shop, configId=identity, product=spu, name=item['name'],
                                priceCents=None, feeCents=None, installment=0, parts=parts,
                                maintenance=dict(path=path, sheet=sheet, row=item['row'], spu=spu, sku=sku)))
            source_configs += 1
        summaries.append(dict(**source, sha256=data['sha256'], count=source_configs,
                              excludedHeadings=headings, excludedWithoutSpu=without_spu))
    if not configs:
        issues.append(dict(path='', sheet='', shop_id='', row=0, reason='维护表没有可监控的完整 SPU／SKU 配置'))
    if len(configs) > MAX_CONFIGS:
        issues.append(dict(path='', sheet='', shop_id='', row=0, reason=f'配置超过 {MAX_CONFIGS} 套上限'))
    # 多表读取完成后再次核对摘要，前一张表在后续读取期间更新也必须重试。
    if any(digest(item['path']) != item['sha256'] for item in summaries):
        raise ValueError('读取期间维护表已更新，请保存完成后重试')
    return sources, configs, issues, summaries


def inspect_sources(bindings):
    """返回稳定的可确认预览；异常行明确排除，整体来源错误仍阻止绑定，不写文件或数据库。"""
    sources, configs, issues, summaries = _read_sources(bindings)
    return dict(sources=sources, files=summaries, count=len(configs),
                goodsCount=len({p['goodsId'] for row in configs for p in row['parts']}),
                shops=sorted({row['shopId'] for row in configs}), issues=issues,
                complete=not any(item['row']==0 or '相同店铺' in item['reason'] for item in issues), skippedRows=len(issues), businessHash=sha(configs))


def read_sources(bindings, previous=None, expected=None):
    """每次巡检读取已保存内容；业务无变化保持修订号，有变化时单调递增。"""
    sources, configs, issues, summaries = _read_sources(bindings)
    # 用户选择按可识别范围监控；缺 SKU、配件 ID 或数量错误行不进入监控清单。
    fatal=[item for item in issues if item['row']==0 or '相同店铺' in item['reason']]
    if fatal:
        first = fatal[0]
        raise ValueError(f"维护表有 {len(fatal)} 项待核对，未更新清单：{first['sheet']} 第 {first['row']} 行 {first['reason']}")
    business_hash = sha(configs)
    if expected is not None:
        # 预览确认与正式绑定使用本次同一解析结果校验，避免两次读取间采用已改变内容。
        current = dict(sources=sources, files=summaries, businessHash=business_hash, complete=True)
        if not isinstance(expected, dict) or any(expected.get(key) != value for key, value in current.items()):
            raise ValueError('维护表在预览后已变化，请重新预览并确认')
    workspace = sha([dict(row, path=os.path.normcase(row['path'])) for row in sources])
    previous = previous or {}
    same_source = previous.get('sourceId') == SOURCE_ID and previous.get('workspaceId') == workspace
    revision = previous.get('revision', 0) if same_source else 0
    if not same_source or previous.get('businessHash') != business_hash:
        revision += 1
    manifest = dict(contract='profit-monitor/v1', schemaVersion=1, complete=True,
                    formulaVersion='profit/v1', sourceId=SOURCE_ID, workspaceId=workspace,
                    revision=revision, publishedAt=datetime.now(timezone.utc).isoformat(),
                    configs=configs, businessHash=business_hash, maintenanceSources=summaries, skippedMaintenanceRows=issues)
    manifest['contentHash'] = sha(manifest)
    if len(canonical(manifest).encode('utf-8')) > MAX_BYTES:
        raise ValueError('维护表清单超过联动快照大小上限，请缩小巡检范围')
    return manifest
