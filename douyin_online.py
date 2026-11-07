"""通过本机 DWS 同步钉钉明细表；保留人工列，不发送群消息或读取凭据。"""
from pathlib import Path
from decimal import Decimal, InvalidOperation
import hashlib
import json
import subprocess
import sys
import time

from douyin_restock import stamp

FIELD_NAMES = ['店铺','商品名称','配置名称','商品链接','平台库存','配件检查','补库存标记',
               '处理状态','备注','平台快照时间','配件核对时间','商品ID','SKU ID','关联键']


def binding(root):
    path = Path(root) / 'online-table.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if not all(value.get(k) for k in ('base_id','table_id','url','profile')) or not set(FIELD_NAMES) <= value.get('fields', {}).keys():
        raise ValueError('在线明细表绑定不完整，请重新配置')
    return value


def status(root, ident):
    path = Path(root) / ('online-sync-' + ident + '.json')
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def save_status(root, ident, value):
    path = Path(root) / ('online-sync-' + ident + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def plain(value):
    """读取文本、单选和链接的业务值，兼容 DWS 的单选对象形式。"""
    if isinstance(value, dict) and 'name' in value:
        return value['name']
    return value


def same(actual, expected):
    """数字字段可能回读为字符串；长商品/SKU 文本编号始终按字符串比较。"""
    actual = plain(actual)
    if type(expected) in (int, float) and isinstance(actual, str):
        try:
            return Decimal(actual) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return actual == expected


def desired(preview, fields):
    result = {}
    if not preview:
        return result
    for row in preview.get('alerts', []):
        key = ':'.join((preview['store_id'], row['product_id'], row['sku_id']))
        from douyin_scope import product_url
        cells = {'店铺':preview['store_name'], '商品名称':row['product_name'], '配置名称':row['sku_name'],
                 '商品链接':dict(text='打开商品', link=product_url(row['product_id'])),
                 '平台库存':row['quantity'], '配件检查':row['erp_state'], '补库存标记':'低库存',
                 '平台快照时间':stamp(preview['inventory_at']), '配件核对时间':stamp(row['parts_checked']),
                 '商品ID':row['product_id'], 'SKU ID':row['sku_id'], '关联键':key}
        result[key] = {fields[name]:value for name,value in cells.items()}
    for group in preview['groups']:
        for sku in group['skus']:
            key = ':'.join((preview['store_id'], group['product_id'], sku['sku_id']))
            cells = {'店铺':preview['store_name'], '商品名称':group['product_name'], '配置名称':sku['sku_name'],
                     '商品链接':dict(text='打开商品', link=group['url']), '平台库存':sku.get('quantity',0), '配件检查':'配件齐全',
                     '补库存标记':('待补库存' if sku.get('quantity',0) == 0 else '低库存'), '平台快照时间':stamp(preview['inventory_at']),
                     '配件核对时间':stamp(sku['parts_checked']), '商品ID':group['product_id'], 'SKU ID':sku['sku_id'], '关联键':key}
            result[key] = {fields[name]:value for name, value in cells.items()}
    return result


def reconcile(config, preview, snapshots, remote, fields):
    """按店铺＋商品＋SKU 对齐，只改程序负责的字段；重复业务键时停止同步。"""
    wanted = desired(preview, fields)
    # 精确身份未匹配项独立入表，不受零库存/配件齐全筛选限制。
    from douyin_scope import product_url
    for sku in snapshots:
        erp = sku.get('erp', {})
        unmatched = erp.get('matching_state') == 'sku_unmatched' or any('维护表未找到唯一' in str(v) for v in erp.get('unknown', []))
        if not unmatched:
            continue
        key = ':'.join((config['id'], sku['product_id'], sku['sku_id']))
        cells = {'店铺':config['name'], '商品名称':sku['product_name'], '配置名称':sku['sku_name'],
                 '商品链接':dict(text='打开商品', link=product_url(sku['product_id'])),
                 '平台库存':sku.get('quantity'), '配件检查':'sku未匹配 待人工核对',
                 '平台快照时间':stamp(config['checked']), '配件核对时间':stamp(erp.get('checked',0)),
                 '商品ID':sku['product_id'], 'SKU ID':sku['sku_id'], '关联键':key}
        wanted[key] = {fields[name]:value for name,value in cells.items()}
    existing = {}
    for row in remote:
        key = plain(row.get('cells', {}).get(fields['关联键']))
        if not isinstance(key, str) or not key.startswith(config['id'] + ':'):
            continue
        if key in existing:
            raise ValueError('在线表格存在重复关联键，已停止同步，请先核对重复行')
        existing[key] = row
    additions, changes = [], []
    for key, cells in wanted.items():
        if key not in existing:
            additions.append(dict(cells={**cells, fields['处理状态']:('sku未匹配 待人工核对' if cells.get(fields['配件检查']) == 'sku未匹配 待人工核对' else '待处理')}))
        else:
            previous = existing[key]
            patch = {fid:value for fid,value in cells.items() if not same(previous['cells'].get(fid), value)}
            if cells.get(fields['配件检查']) == 'sku未匹配 待人工核对' and plain(previous['cells'].get(fields['处理状态'])) == '待处理':
                patch[fields['处理状态']] = 'sku未匹配 待人工核对'
            if patch:
                changes.append(dict(recordId=previous['recordId'], cells=patch))
    # 用户明确要求退出提醒直接删行；只删除本店关联键命中的真实记录 ID。
    deletions = []
    for key, previous in existing.items():
        if key in wanted:
            continue
        deletions.append(previous['recordId'])
    return additions, changes, deletions, wanted


class DwsTable:
    def __init__(self, config):
        self.config = config
        packaged = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'dws.exe'
        self.executable = packaged if packaged.is_file() else Path.home() / '.local/bin/dws.exe'

    def call(self, command):
        result = subprocess.run([str(self.executable), 'aitable', *command, '--base-id', self.config['base_id'],
            '--table-id', self.config['table_id'], '--profile', self.config['profile'], '--format', 'json', '--yes'],
            capture_output=True, text=True, encoding='utf-8', timeout=90,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not result.stdout.strip():
            raise ValueError('在线表格操作未确认成功，请检查 DWS 登录或表格权限')
        value = json.loads(result.stdout)
        if value.get('success') is False or value.get('ok') is False or value.get('status') in ('error','failed','partial_success') or value.get('error'):
            raise ValueError('在线表格操作未确认成功，已保留本地结果')
        return value.get('data', value)

    def query(self):
        value = self.call(['record','query','--all','--page-limit','50'])
        if value.get('hasMore') or value.get('complete') is not True:
            raise ValueError('在线表格未完整读取，本轮不写入')
        return value.get('records') or []

    def write(self, operation, records):
        # 小批写入并限制命令行长度，避免 Windows 截断长 JSON。
        batch = []
        for record in records:
            if batch and (len(batch) >= 10 or len(json.dumps(batch + [record], ensure_ascii=False)) > 20000):
                self.call(operation + ['--records', json.dumps(batch, ensure_ascii=False)]); batch=[]
            if len(json.dumps(record, ensure_ascii=False)) > 20000:
                raise ValueError('单条明细过长，已停止写入，未截断原始数据')
            batch.append(record)
        if batch:
            self.call(operation + ['--records', json.dumps(batch, ensure_ascii=False)])

    def delete(self, ids):
        for start in range(0, len(ids), 100):
            self.call(['record','delete','--record-ids',','.join(ids[start:start + 100])])


def verified(rows, expected, deleted_ids, key_field):
    """完整回读确认全部目标值以及删除结果；重复关联键不能当作成功。"""
    by_key = {}
    for row in rows:
        if row['recordId'] in deleted_ids:
            return False
        key = plain(row.get('cells',{}).get(key_field))
        if key in expected:
            if key in by_key:
                return False
            by_key[key] = row
    return all(key in by_key and all(same(by_key[key]['cells'].get(fid), value) for fid,value in cells.items())
               for key,cells in expected.items())


def sync_online(store, ident, *, force=False, client=None):
    """同步后回读业务字段；写入结果未知时阻止自动重放，防止重复新增。"""
    config = binding(store.root)
    if not config:
        return dict(ok=False, message='尚未配置在线明细表')
    shop = store.store(ident); preview = store.restock_preview(ident); snapshots = store.snapshots(ident)
    fingerprint = hashlib.sha256(json.dumps([preview, snapshots, config], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    previous = status(store.root, ident)
    if not force and previous.get('ok') and previous.get('fingerprint') == fingerprint:
        return previous
    if previous.get('uncertain') and not force:
        return dict(previous, message='上次在线写入结果待核实，已停止自动重试')
    client = client or DwsTable(config)
    receipt = dict(previous if previous.get('uncertain') else {}, ok=False,
                   message='正在同步在线明细…', fingerprint=fingerprint, at=time.time())
    save_status(store.root, ident, receipt)
    writing = bool(previous.get('uncertain'))
    try:
        before = client.query()
        additions, changes, deletions, expected = reconcile(shop, preview, snapshots, before, config['fields'])
        if previous.get('uncertain'):
            # 手动同步先核实上次已提交操作，未核实前不重新创建、修改或删除。
            if not verified(before, previous.get('intent',expected), previous.get('delete_ids',[]), config['fields']['关联键']):
                receipt = dict(previous, message='上次在线写入仍待核实；未重复写入，请稍后再同步')
                save_status(store.root, ident, receipt); return receipt
            writing=False
        if additions or changes or deletions:
            # 进程中断也保留“结果未知”状态，启动后不会直接重发创建请求。
            receipt.update(uncertain=True,intent=expected,delete_ids=deletions)
            save_status(store.root, ident, receipt); writing=True
            if additions:
                client.write(['record','create'], additions)
            if changes:
                client.write(['+record-update'], changes)
            if deletions:
                client.delete(deletions)
            after = client.query()
        else:
            after = before
        if not verified(after, expected, deletions, config['fields']['关联键']):
            raise ValueError('在线表格回读未通过，已保留本地结果，未自动重试')
        receipt = dict(ok=True, message=f"在线明细已同步：{len(expected)} 个 SKU（含未匹配待核对项）",
                       fingerprint=fingerprint, at=time.time(), added=len(additions), updated=len(changes), deleted=len(deletions), url=config['url'])
    except Exception as error:
        receipt.update(ok=False, uncertain=writing, message=str(error) if isinstance(error, ValueError) else '在线同步未完成，请检查钉钉登录；本地记录已保留')
    save_status(store.root, ident, receipt)
    return receipt
