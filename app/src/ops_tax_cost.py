"""从已选择的本机工作簿读取含税核算成本，只按 ERP 商品 ID 关联。"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile
import re
import unicodedata

from douyin_maintenance import workbook

KEY = 'ops_tax_cost_binding'
ID_HEADERS = {'goods_id','goodsid','商品id','erp商品id','erp商品编号','erp编号','erp商品编码'}
COST_HEADERS = {'含税成本','含税核算成本','核算成本','含税单价'}
MAX_ROWS = 100000
MAX_FILE = 160 * 1024 * 1024


def header(value):
    return re.sub(r'[\s()]', '', unicodedata.normalize('NFKC', value).casefold()) if isinstance(value, str) else ''


def metadata(path):
    stat = Path(path).stat()
    if not 0 < stat.st_size <= MAX_FILE:
        raise ValueError('成本工作簿过大或为空')
    return stat.st_size, stat.st_mtime_ns


def locate(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请先选择主机成本工作簿')
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() not in ('.xlsx','.xlsb','.xlsm') or not path.is_file():
        raise ValueError('请选择存在的 .xlsx、.xlsb 或 .xlsm 成本工作簿')
    return path


def candidates(value):
    path = locate(value)
    start = metadata(path)
    found = []
    try:
        with workbook(path) as (sheets, rows):
            for sheet in sheets:
                if sheet == '禁止编辑':
                    continue
                for number, row in rows(sheet):
                    if number > 12:
                        break
                    ids = [(i, name) for i, name in row.items() if header(name) in ID_HEADERS]
                    costs = [(i, name) for i, name in row.items() if header(name) in COST_HEADERS]
                    if not ids or not costs:
                        continue
                    for cost_col, cost_label in costs:
                        # 同名 ID 列不能自动选第一列；相邻的成本数据块优先列出。
                        for id_col, id_label in sorted(ids, key=lambda item: (item[0]>cost_col,abs(cost_col-item[0]))):
                            found.append(dict(path=str(path), sheet=sheet, header_row=number,
                                              id_col=id_col, id_label=id_label, cost_col=cost_col, cost_label=cost_label))
                    if len(found) > 40:
                        raise ValueError('候选列过多，请使用只包含成本数据的工作簿')
                    break
    except (OSError, KeyError, TypeError, BadZipFile, ParseError):
        raise ValueError('成本工作簿无法读取，请先确认文件已保存且可访问') from None
    if metadata(path) != start:
        raise ValueError('成本表在识别时已变化，请重新选择')
    if not found:
        raise ValueError('工作簿前 12 行未找到 ERP 商品 ID 和含税成本表头')
    return found


def goods_id(value):
    if type(value) in (int,float):
        # Excel 数值超过 15 位可能丢精度；长商品 ID 必须为文本。
        if not 1 <= value < 10**15 or int(value) != value:
            return ''
        value = str(int(value))
    if isinstance(value,str):
        value = value.strip()
        if re.fullmatch(r'[1-9][0-9]{0,19}', value):
            return value
    return ''


def price_cents(value):
    if isinstance(value, bool) or value in (None,''):
        return None
    try:
        value = Decimal(str(value).strip())
        if not value.is_finite() or not 0 <= value < Decimal('100000000'):
            return None
        return int((value*100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


@lru_cache(maxsize=4)
def load_index(path, sheet, header_row, id_col, id_label, cost_col, cost_label, fingerprint):
    amounts = {}
    missing = set()
    with workbook(path) as (sheets, rows):
        if sheet not in sheets:
            raise ValueError('成本工作表已改名或移除')
        checked = False
        for number, row in rows(sheet):
            if number > MAX_ROWS:
                raise ValueError('成本表超过 10 万行读取上限')
            if number < header_row:
                continue
            if not checked:
                if number != header_row or header(row.get(id_col)) != header(id_label) or header(row.get(cost_col)) != header(cost_label):
                    raise ValueError('成本表的表头已变化，请重新识别列')
                checked = True
                continue
            gid = goods_id(row.get(id_col))
            if not gid:
                continue
            value = price_cents(row.get(cost_col))
            if value is None:
                missing.add(gid)
                continue
            amounts.setdefault(gid,set()).add(value)
    if not checked:
        raise ValueError('成本表头不存在')
    if metadata(path) != fingerprint:
        raise ValueError('读取期间工作簿发生变化，请稍后再查')
    return ({k:tuple(sorted(v)) for k,v in amounts.items()}, frozenset(missing))


def open_index(binding):
    fingerprint = metadata(binding['path'])
    data, missing = load_index(*(binding[k] for k in ('path','sheet','header_row','id_col','id_label','cost_col','cost_label')),
                               fingerprint)
    return data, missing, fingerprint


def quote_costs(service, ids):
    with service.db() as db:
        binding = service.get(db, KEY)
    if not binding:
        return {gid:dict(tax_cost_status='not_bound',tax_cost_cents=None) for gid in ids}
    try:
        data, missing, fingerprint = open_index(binding)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile, ParseError):
        return {gid:dict(tax_cost_status='read_failed',tax_cost_cents=None) for gid in ids}
    out = {}
    for gid in ids:
        choices = data.get(gid,())
        state = ('conflict' if len(choices)>1 else 'ready' if choices else 'unknown' if gid in missing else 'no_match')
        out[gid] = dict(tax_cost_status=state,tax_cost_cents=choices[0] if state=='ready' else None,
                        tax_cost_observed_at=fingerprint[1]/1_000_000_000,
                        tax_cost_source=Path(binding['path']).name)
    return out
