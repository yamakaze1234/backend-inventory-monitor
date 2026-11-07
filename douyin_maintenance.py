"""只读已保存的主机维护表：精确匹配 SPU／SKU，不运行 Excel 宏或读取登录凭据。"""
from collections import Counter, OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote
import hashlib
import json
import re
import threading
import time

KNOWN_SHOPS = {'英特尔官方旗舰店': '76', '技嘉旗舰店': '32', '乔思伯官方旗舰店': '33'}
_CACHE = OrderedDict()
_LOCK = threading.Lock()


def text(value):
    """配件编号允许 Excel 整数缓存；长商品编号另行强制文本校验。"""
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def positive(value):
    value = text(value)
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError('配件编号或数量不是正整数')
    return int(value)


def platform_id(value):
    # Excel 数字型长 ID 可能已丢精度；不猜测修复，不用名称兜底。
    return value.strip() if isinstance(value, str) and re.fullmatch(r'[0-9]{15,20}', value.strip()) else ''


@contextmanager
def workbook(path):
    """支持本机或共享盘的 xlsb/xlsx/xlsm，仅读取已保存的公式缓存。"""
    path = Path(path)
    if not path.is_file() or path.suffix.lower() not in ('.xlsb', '.xlsx', '.xlsm'):
        raise ValueError('请选择存在的 .xlsb、.xlsx 或 .xlsm 主机维护表')
    if path.suffix.lower() == '.xlsb':
        from pyxlsb import open_workbook
        with open_workbook(str(path)) as book:
            def rows(sheet):
                with book.get_sheet(sheet) as source:
                    for cells in source.rows():
                        if cells:
                            yield cells[0].r + 1, {c.c: c.v for c in cells}
            yield book.sheets, rows
    else:
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            def rows(sheet):
                for number, values in enumerate(book[sheet].iter_rows(values_only=True), 1):
                    yield number, dict(enumerate(values))
            yield book.sheetnames, rows
        finally:
            book.close()


def settings(rows):
    """只取店铺编号、固定附加明细和电源线机箱名单，其他设置不提取。"""
    selected = {}
    for number, values in rows('禁止编辑'):
        if number > 35:
            break
        for column in (6, 8):
            if (number, column) in ((2, 6), (6, 8)) or (26 <= number <= 35 and column == 6):
                selected[number, column] = text(values.get(column))
    shop_id = str(positive(selected.get((2, 6))))
    try:
        extras = json.loads('[' + unquote(selected.get((6, 8), '')).lstrip(',') + ']')
        for part in extras:
            positive(part.get('goods_id')); positive(part.get('n'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('维护表固定附加明细格式无效，请检查禁止编辑!I6') from None
    return shop_id, extras, {selected.get((r, 6)) for r in range(26, 36)} - {None, ''}


def headers(rows, sheet):
    for number, values in rows(sheet):
        if number > 10:
            break
        labels = {c: text(v) for c, v in values.items()}
        if {'SPU', 'SKU', '简称', 'CPU_id', '机箱_id'} <= set(labels.values()):
            required = {'SPU', 'SKU', '简称'} | {v for v in labels.values() if v.endswith(('_id', '_数量'))}
            if any(list(labels.values()).count(v) != 1 for v in required):
                raise ValueError('维护表存在重复的编号或数量表头')
            return number, {v: c for c, v in labels.items() if v}
    raise ValueError('工作表前十行未找到 SPU、SKU、简称及配件编号表头')


def inspect_maintenance(path):
    """供选择文件窗口展示候选配置页，不自动为任何店铺保存绑定。"""
    with workbook(path) as (sheets, rows):
        if '禁止编辑' not in sheets:
            raise ValueError('维护表缺少“禁止编辑”工作表，无法核对店铺身份')
        shop_id, _, _ = settings(rows)
        candidates = []
        for sheet in sheets:
            if sheet == '禁止编辑':
                continue
            try:
                headers(rows, sheet)
                candidates.append(sheet)
            except ValueError:
                continue
        if not candidates:
            raise ValueError('没有找到含 SKU 配件字段的配置工作表')
    return dict(path=str(Path(path).resolve()), sheets=candidates, shop_id=shop_id)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_maintenance(path, sheet, shop_id):
    """同一已保存文件复用解析缓存；更新后重新读取，读取中变更则拒绝采用。"""
    path = Path(path).resolve()
    before = digest(path)
    key = (str(path), sheet, str(shop_id), before)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    result = {}
    with workbook(path) as (sheets, rows):
        if sheet not in sheets or '禁止编辑' not in sheets:
            raise ValueError('所选工作表不存在或维护表结构不完整')
        actual, extras, cable_cases = settings(rows)
        if actual != str(shop_id):
            raise ValueError('维护表内店铺编号与已保存设置不一致')
        header_row, columns = headers(rows, sheet)
        slots = [name[:-3] for name in columns if name.endswith('_id')]
        for number, values in rows(sheet):
            if number <= header_row:
                continue
            get = lambda name: values.get(columns.get(name, -1))
            pid, sid = platform_id(get('SPU')), platform_id(get('SKU'))
            if not pid or not sid:
                continue
            row_key = pid + ':' + sid
            if row_key in result:
                result[row_key] = dict(state='维护表同一 SPU／SKU 有重复行', row=number)
                continue
            parts, names = Counter(), {}
            item = dict(state='ready', row=number, name=text(get('简称')))
            try:
                for slot in slots:
                    gid, name = text(get(slot + '_id')), text(get(slot))
                    # 已核实的显示器公式文本：引用编号为空时 IF 的结果确定为空。
                    # 只识别此固定公式，不执行表格文本或猜测其他公式。
                    empty_monitor_formula = '=IF([@[配件6_id]]<>"",INDEX([主机成本表V2.xlsb]库存数据!$BD:$BD,MATCH([@[配件6_id]],[主机成本表V2.xlsb]库存数据!$BB:$BB,0)),"")'
                    if slot == '显示器' and not gid and '配件6_id' in columns and not text(get('配件6_id')) and name == empty_monitor_formula:
                        name = ''
                    if slot + '_数量' not in columns:
                        raise ValueError('缺少配件数量列：' + slot)
                    if not gid:
                        if slot == '显卡' and name in ('不含显卡，可咨询客服加装独显使用', '无独立显卡', '不含独立显卡', '全新Intel ARC Xe核显 支持8K 60HZ视频输出'):
                            continue
                        if name not in ('', '0'):
                            raise ValueError('配件有名称但没有编号：' + slot)
                        continue
                    gid = str(positive(gid)); parts[gid] += positive(get(slot + '_数量')); names[gid] = name
                if not parts:
                    raise ValueError('配置没有有效配件')
                if text(get('机箱_id')) in cable_cases:
                    parts['1651'] += positive(get('机箱_数量')); names['1651'] = '主机电源线'
                for extra in extras:
                    parts[str(positive(extra['goods_id']))] += positive(extra['n'])
                item.update(parts=dict(parts), names=names)
            except ValueError as error:
                item.update(state=str(error))
            result[row_key] = item
    if digest(path) != before:
        raise ValueError('读取期间维护表已更新，请保存完成后重试')
    value = dict(rows=result, source=str(path), sheet=sheet, shop_id=str(shop_id), sha256=before,
                 modified=path.stat().st_mtime, observed=time.time())
    with _LOCK:
        _CACHE[key] = value
        while len(_CACHE) > 3:
            _CACHE.popitem(last=False)
    return value
