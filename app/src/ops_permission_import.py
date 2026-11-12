"""运营机器人权限表的模板、校验和合并预览。"""
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from ops_command_api import PRICE_ACCESS, checked_id
from ops_commands import COMMANDS

HEADERS = ('钉钉成员ID', '允许的指令ID', '允许的店铺ID', '附加权限ID')
KEYS = ('user_id', 'commands', 'shops', 'fields')
SHEET = '成员权限'
LIMIT = 200
FIELDS = dict(erp_cost='ERP 成本和成本变化', maintenance_price='维护表价格', tax_cost='含税（核算）成本',
              operator='订单操作人', table_links='授权表格入口')


def write_template(path, selected, shops):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    book = Workbook()
    data = book.active
    data.title = SHEET
    data.append(HEADERS)
    data.freeze_panes = 'B2'
    data.auto_filter.ref = 'A1:D201'
    # 成员和店铺编号都以文本保存，避免长数字被 Excel 改写。
    for col, width in zip('ABCD', (27, 55, 28, 44)):
        data.column_dimensions[col].width = width
    for row in data.iter_rows(min_row=2, max_row=LIMIT + 1, min_col=1, max_col=4):
        for cell in row:
            cell.number_format = '@'
    for cell in data[1]:
        cell.fill = PatternFill('solid', fgColor='163352')
        cell.font = Font(color='FFFFFF', bold=True)
    guide = book.create_sheet('填写说明')
    guide.column_dimensions['A'].width = 27
    guide.column_dimensions['B'].width = 45
    guide.column_dimensions['C'].width = 27
    for row in (
        ('项目', '填写说明', '当前群状态'),
        ('钉钉成员ID', '填写本组织已核实的成员 ID；成员一人一行。'),
        ('指令、店铺、附加权限', '填写对应 ID，多项用逗号分隔。'),
        ('导入规则', '只合并本表中列出的成员；未列成员保留。'),
        ('保留或清空权限', '同一成员整行替换；留空的权限列表示清空。指令列留空表示无指令权限。'),
        ('示例', '指令：help,inventory.get,price.changes；附加权限：erp_cost。'),
        ('指令 ID', '功能', '当前群是否勾选'),
        *((key, label, '已勾选' if key in selected else '未勾选') for key, (label, _) in COMMANDS.items()),
        ('附加权限 ID', '显示范围', ''),
        *((key, label, '') for key, label in FIELDS.items()),
        ('店铺 ID', '当前工作区可选店铺', ''),
        *((key, label, '') for key, label in sorted(shops.items())),
    ):
        guide.append(row)
    guide.freeze_panes = 'A2'
    for row in guide:
        for cell in row:
            cell.alignment = Alignment(vertical='center', wrap_text=True)
        guide.row_dimensions[row[0].row].height = 31
    for cell in guide[1]:
        cell.fill = PatternFill('solid', fgColor='163352')
        cell.font = Font(color='FFFFFF', bold=True)
    try:
        book.save(path)
    finally:
        book.close()


def read_rows(path):
    from openpyxl import load_workbook

    if not isinstance(path, str) or not path.strip():
        raise ValueError('请选择权限模板填写后的 .xlsx 文件')
    path = Path(path)
    if path.suffix.lower() != '.xlsx' or not path.is_file() or path.stat().st_size > 4_000_000:
        raise ValueError('请使用不超过 4 MB 的 .xlsx 权限表格')
    try:
        with ZipFile(path) as archive:
            if sum(r.file_size for r in archive.infolist()) > 24_000_000:
                raise ValueError('权限表格解压后过大')
        book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, OSError, KeyError):
        raise ValueError('权限表格无法读取，请重新下载模板') from None
    try:
        if SHEET not in book.sheetnames:
            raise ValueError('找不到「成员权限」工作表，请使用模板')
        sheet = book[SHEET]
        # 不依赖可能被制表软件误写的工作表范围；限制解析规模。
        sheet.reset_dimensions()
        sheet.calculate_dimension(force=True)
        if (sheet.max_row or 0) > 1000 or (sheet.max_column or 0) > 5:
            raise ValueError('权限表格范围超出模板，请将内容复制到空白模板后重试')
        rows = sheet.iter_rows(min_col=1, max_col=5)
        header = next(rows, ())
        if tuple(getattr(c, 'value', None) for c in header[:4]) != HEADERS or header[4].value not in (None, ''):
            raise ValueError('权限表格表头与模板不一致')
        result, errors = [], []
        for line, cells in enumerate(rows, 2):
            vals = [getattr(c, 'value', None) for c in cells]
            if not any(v not in (None, '') for v in vals):
                continue
            if line > 1000 or len(result) + len(errors) >= LIMIT:
                raise ValueError('每次最多导入 200 名成员，请分批导入')
            if vals[4] not in (None, '') or any(getattr(c, 'data_type', '') in ('f', 'e') or
                                                (v is not None and not isinstance(v, str)) for c, v in zip(cells, vals)):
                errors.append(dict(line=line, reason='请按模板填写 4 列纯文本，不使用公式或数值格式的编号'))
                continue
            if any(len(v or '') > 2000 for v in vals):
                errors.append(dict(line=line, reason='单元格内容过长'))
                continue
            result.append((line, dict(zip(KEYS, (v or '' for v in vals[:4])))))
        if not result and not errors:
            raise ValueError('表格没有可导入的成员')
        return result, errors
    finally:
        book.close()


def member_rights(row, split, allowed_shops):
    user = row.get('user_id')
    if not isinstance(user, str) or not checked_id(user.strip()):
        raise ValueError('成员 ID 为空或格式无效')
    rights, shops, fields = (split(row.get(key)) for key in KEYS[1:])
    if not set(rights) <= COMMANDS.keys():
        raise ValueError('包含未知的指令 ID')
    if not set(fields) <= PRICE_ACCESS:
        raise ValueError('包含未知的附加权限 ID')
    if not set(shops).issubset(allowed_shops):
        raise ValueError('店铺 ID 不在当前工作区的可配置范围内')
    return user.strip(), dict(commands=rights, shops=shops, fields=fields)


def inspect(path, existing, split, shops):
    parsed, errors = read_rows(path)
    merged, changes, seen = dict(existing), [], set()
    for line, row in parsed:
        try:
            user, rights = member_rights(row, split, shops)
            if user in seen:
                raise ValueError('同一成员 ID 在表格内重复')
        except ValueError as error:
            errors.append(dict(line=line, reason=str(error)))
            continue
        seen.add(user)
        kind = '新增' if user not in existing else '无变化' if existing[user] == rights else '变更'
        merged[user] = rights
        changes.append(dict(line=line, user_id=user, operation=kind,
                            **{k: '、'.join(v) or '无' for k, v in rights.items()}))
    if len(merged) > LIMIT:
        errors.append(dict(line='—', reason='合并后超过单群 200 名成员上限'))
    return merged, changes, sorted(errors, key=lambda r: int(r['line']) if isinstance(r['line'], int) else LIMIT + 1000)
