"""主机成本表的选择、核对和保存，仅写本机连接设置。"""
import json
from pathlib import Path

from openpyxl.utils.cell import get_column_letter

from inventory_business_views import A, F, T
from ops_tax_cost import KEY, candidates, metadata, open_index


def describe(binding):
    id_col, price_col = (get_column_letter(binding[k]+1) for k in ('id_col','cost_col'))
    return f"{binding['sheet']} · {id_col} {binding['id_label']} → {price_col} {binding['cost_label']}"


def panel(b):
    with b.service.db() as db:
        binding = b.service.get(db,KEY)
    forms = [A('ops-cost-inspect', '连接主机成本表', [F('path','主机成本表','file',pickOnly=True)],
               module='settings',submitLabel='识别工作表与列',
               description='选择本机或已挂载共享目录中的 .xlsx、.xlsb 或 .xlsm 文件。只读已保存值，识别后还要预览并确认。')]
    tables = []
    if binding:
        tables.append(T('主机成本表连接', [('path','工作簿'),('columns','工作表和列')],
                        [dict(path=binding['path'],columns=describe(binding),_forms=[
                            A('ops-cost-disconnect','解除成本表连接',confirm='停用此来源。原工作簿保持不变。')])]))
    return forms, tables


def inspect(b, values):
    found = candidates(values.get('path'))
    token = b.review('ops-cost-candidates',found)
    return dict(message='请选择对应的工作表及列；同名商品 ID 列按列字母区分。',
                nextForm=A('ops-cost-preview','选择成本工作表及列',
                           [F('choice','工作表及匹配列',options=[(str(i),describe(r)) for i,r in enumerate(found)])],
                           dict(token=token,choice=''),module='settings',submitLabel='预览成本数据'))


def preview(b, values):
    found = b.reviewed(values.get('token'),'ops-cost-candidates')
    choice = values.get('choice')
    if not isinstance(choice,str) or not choice.isascii() or not choice.isdecimal() or len(choice)>3 or int(choice)>=len(found):
        raise ValueError('请选择要连接的工作表及列')
    binding = found[int(choice)]
    data, _, fingerprint = open_index(binding)
    known = {}
    with b.service.db() as db:
        before = b.service.get(db,KEY)
        for raw, in db.execute('SELECT data FROM catalog'):
            row=json.loads(raw)
            gid=str(row.get('goods_id',''))
            if gid in data and gid not in known:
                known[gid]=row.get('name','')
    samples = []
    for gid in (known.keys() if known else data.keys()):
        if len(data[gid]) != 1:
            continue
        amount = data[gid][0]
        samples.append(dict(goods_id=gid,name=known.get(gid,'待目录核对'),amount=f'¥{amount//100:,}.{amount%100:02}'))
        if len(samples) >= 5:
            break
    if not samples:
        raise ValueError('所选列没有可核对的商品成本，请返回重新选列')
    token = b.review('ops-cost-save',(binding,before,fingerprint))
    return dict(message=f'{Path(binding["path"]).name}：{describe(binding)}。'
                        f'可核对的商品成本 {sum(len(v)==1 for v in data.values())} 项，'
                        f'目录匹配 {len(known)} 项，同 ID 不同成本 {sum(len(v)>1 for v in data.values())} 项。尚未保存。',
                tables=[T('成本数据预览', [('goods_id','ERP 商品 ID'),('name','ERP 商品名称'),('amount','含税（核算）成本')],samples)],
                forms=[A('ops-cost-save','保存成本表连接',values=dict(token=token),module='settings',
                         confirm='保存已预览的工作表与列。有「含税（核算）成本」权限的成员查询商品时可查看。')])


def save(b,values):
    binding,before,fingerprint=b.reviewed(values.get('token'),'ops-cost-save')
    if metadata(binding['path']) != fingerprint:
        raise ValueError('工作簿在预览后已更新，请重新识别')
    with b.service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if b.service.get(db,KEY)!=before:
            raise ValueError('成本表连接已变化，请重新预览')
        b.service.put(db,KEY,binding)
        if b.service.get(db,KEY)!=binding:
            raise RuntimeError('成本表连接回读不一致')
    return dict(message='成本表连接已保存：'+describe(binding))


def disconnect(b):
    with b.service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        b.service.put(db,KEY,None)
    return dict(message='已解除成本表连接')
