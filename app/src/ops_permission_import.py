"""按姓名和功能名称生成权限表；导入仍需核实组织身份、预览并确认。"""
from collections import Counter
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from ops_commands import COMMANDS

SHEET = '成员权限'
LIMIT = 200
FIELDS = dict(erp_cost='ERP 成本和成本变化', maintenance_price='维护表价格', tax_cost='含税（核算）成本',
              operator='订单操作人', table_links='授权表格入口')


def shop_labels(shops):
    counts = Counter(shops.values())
    return {key: f'{label}（{key}）' if counts[label]>1 else label for key,label in shops.items()}


def columns(shops):
    # 三类权限加前缀，工作表可见列只用业务名称；同名店铺仍能唯一选中。
    return [('commands', k, '功能｜'+label) for k,(label,_) in COMMANDS.items()] + [
        ('shops', k, '店铺｜'+label) for k,label in sorted(shop_labels(shops).items())] + [
        ('fields', k, '附加｜'+label) for k,label in FIELDS.items()]


def write_template(path, group, shops, roster):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook()
    data = book.active
    data.title = SHEET
    options = book.create_sheet('成员选项')
    options['B1'] = group
    options.sheet_state = 'hidden'
    for row, (label, member) in enumerate(roster.items(), 1):
        cell = options.cell(row, 1, label)
        cell.data_type = 's'  # 昵称可能以等号开头，只写成纯文本。
        options.cell(row, 3, member['open_id']).data_type = 's'
    book.defined_names.add(DefinedName('GroupMembers', attr_text="'成员选项'!$A$1:$A$"+str(len(roster))))
    checks = columns(shops)
    data.append(['群成员', *(label for _,_,label in checks)])
    last = get_column_letter(len(checks)+1)
    data.freeze_panes = 'B2'
    data.auto_filter.ref = f'A1:{last}{LIMIT+1}'
    data.row_dimensions[1].height = 32
    for index, cell in enumerate(data[1], 1):
        data.column_dimensions[cell.column_letter].width = 29 if index == 1 else 20
        cell.fill = PatternFill('solid', fgColor='163352')
        cell.font = Font(color='FFFFFF', bold=True)
        cell.alignment = Alignment(vertical='center', wrap_text=True)
    # Excel 不依赖宏；用数据验证下拉在 Excel/WPS 中选择姓名和“是”。
    for formula, area, error in (('GroupMembers', f'A2:A{LIMIT+1}', '请从名单中选择群成员'),
                                 ('"是"', f'B2:{last}{LIMIT+1}', '请选择「是」，或清空此格')):
        choice = DataValidation(type='list', formula1=formula, allow_blank=True, showErrorMessage=True,
                                errorStyle='stop', errorTitle='请选择列表选项', error=error)
        data.add_data_validation(choice)
        choice.add(area)
    guide = book.create_sheet('填写说明')
    guide.column_dimensions['A'].width = 87
    for text in ('1. 在「成员权限」页的第一列选择群成员，一人一行。',
                 '2. 要授权的功能、店铺和附加权限选择「是」。留空表示该项不授权。',
                 '3. 保存文件后回到原群导入，先看变更预览，再确认。',
                 '表格中列出的成员以整行勾选结果为准；未列出的成员保持原权限。',
                 '群成员、功能列或店铺有变化时，请重新下载模板。模板包含群成员姓名，请仅在本部门使用。'):
        guide.append([text])
    try:
        book.save(path)
    finally:
        book.close()


def read_rows(path, group, shops):
    from openpyxl import load_workbook

    if not isinstance(path, str) or not path.strip():
        raise ValueError('请选择填写后的 .xlsx 权限表格')
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
        if SHEET not in book.sheetnames or '成员选项' not in book.sheetnames:
            raise ValueError('请为当前群重新下载新版权限模板')
        if book['成员选项']['B1'].value != group:
            raise ValueError('此模板属于另一个群，请从当前群下载模板')
        saved_roster = {}
        for label, _, open_id in book['成员选项'].iter_rows(min_col=1, max_col=3, max_row=20000, values_only=True):
            if isinstance(label, str) and isinstance(open_id, str):
                saved_roster[label] = open_id
        checks = columns(shops)
        sheet = book[SHEET]
        # 不信任文档声称的范围；保留批量导入上限。
        sheet.reset_dimensions()
        sheet.calculate_dimension(force=True)
        width = len(checks)+1
        if (sheet.max_row or 0) > 1000 or (sheet.max_column or 0) > width+1:
            raise ValueError('表格范围已改变，请将内容复制到新版模板')
        rows = sheet.iter_rows(min_col=1, max_col=width+1)
        header = tuple(c.value for c in next(rows, ()))
        if header != ('群成员', *(label for _,_,label in checks), None):
            raise ValueError('权限列或店铺已变化，请重新下载当前群的权限模板')
        result, errors = [], []
        for line, cells in enumerate(rows, 2):
            vals = [c.value for c in cells]
            if not any(v not in (None, '') for v in vals):
                continue
            if len(result)+len(errors)>=LIMIT:
                raise ValueError('每次最多导入 200 名成员，请分批导入')
            member = vals[0]
            if (vals[-1] not in (None,'') or not isinstance(member, str) or not 0 < len(member) <= 190
                    or any(c.data_type in ('f','e') for c in cells)):
                errors.append(dict(line=line, reason='请在首列选择成员，其余列只选择「是」或留空'))
                continue
            rights = dict(commands=[], shops=[], fields=[])
            if any(v not in ('是',None,'') for v in vals[1:-1]):
                errors.append(dict(line=line, reason='权限只能选择「是」或留空'))
                continue
            for value,(key,ident,_) in zip(vals[1:-1], checks):
                if value == '是':
                    rights[key].append(ident)
            result.append((line,member,saved_roster.get(member),rights))
        if not result and not errors:
            raise ValueError('表格没有可导入的成员')
        return result, errors
    finally:
        book.close()


def inspect(parsed, errors, existing, roster, resolve, shops):
    merged, changes, names, seen = dict(existing), [], {}, set()
    shop_names = shop_labels(shops)
    for line, label, open_id, rights in parsed:
        try:
            if not open_id or label not in roster or roster[label]['open_id'] != open_id:
                raise ValueError('成员已不在当前名单或姓名已更新，请重新下载模板')
            user = resolve(roster[label])
            if user in seen:
                raise ValueError('同一成员在表格内重复')
        except ValueError as error:
            errors.append(dict(line=line, reason=str(error)))
            continue
        seen.add(user)
        names[user] = label
        before = existing.get(user)
        same = before is not None and all(set(before[k]) == set(v) for k,v in rights.items())
        kind = '新增' if before is None else '无变化' if same else '变更'
        merged[user] = before if same else rights
        changes.append(dict(line=line, member=label, operation=kind,
                            commands='、'.join(COMMANDS[k][0] for k in rights['commands']) or '无',
                            shops='、'.join(shop_names[k] for k in rights['shops']) or '无',
                            fields='、'.join(FIELDS[k] for k in rights['fields']) or '无'))
    if len(merged)>LIMIT:
        errors.append(dict(line='—', reason='合并后超过单群 200 名成员上限'))
    return merged, changes, names, sorted(errors,key=lambda r:int(r['line']) if isinstance(r['line'],int) else LIMIT+1000)
