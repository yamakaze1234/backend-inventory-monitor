"""固定日报导出：仅处理已经校验的隔离输出，不访问数据库或消息接口。"""
import json
import sys
from pathlib import Path
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill


def render(directory):
    root = Path(directory)
    data = json.loads((root/'source.json').read_text(encoding='utf-8'))
    book = Workbook()
    book.remove(book.active)
    def sheet(name, headers, rows):
        target = book.create_sheet(name)
        target.append(headers)
        for row in rows:
            target.append(row)
            # ERP 字符串按文本保存，不能被 Excel 解释为公式。
            for cell in target[target.max_row]:
                if isinstance(cell.value, str): cell.data_type = 's'
        target.freeze_panes = 'A2'
        target.auto_filter.ref = target.dimensions
        for cell in target[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='17365D')
        for column in target.columns:
            target.column_dimensions[column[0].column_letter].width = 24
    fields = ['出库时间','单据类型','出库单号','网店单号','店铺','项目组','整机数量','销售金额','处理器','显卡系列','机箱','销售类目']
    heads = ['单据类型','明细行数','整机数量','销售金额','缺失数量','缺失金额']
    sheet('日报总览', heads, [[r.get(k) for k in heads] for r in data['summary']])
    sheet('出库明细', fields, [[r.get(k) for k in fields] for r in data['rows']])
    for name,key,labels in [('店铺汇总','stores',['店铺']),('CPU显卡组合','combos',['处理器','显卡系列'])]:
        sheet(name,labels+['销售数量','退货数量','净金额','金额缺失行数'],[r['values']+[r[k] for k in ['salesQty','returnQty','netAmount','missingAmount']] for r in data[key]])
    path=root/f"抖英特出库日报-{data['date']}.xlsx"
    if path.exists(): raise ValueError('输出文件已存在')
    book.save(path)
    check=load_workbook(path,read_only=True)
    assert check['出库明细'].max_row == len(data['rows'])+1
    check.close()


if __name__ == '__main__': render(sys.argv[1])
