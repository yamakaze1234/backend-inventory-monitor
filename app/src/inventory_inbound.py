"""Validated warehouse-ledger entries. A transfer is not a receipt."""
import hashlib
import json
from inventory_rules import quantity


def apply_journal(db, product, current, old, entries, emit):
    if not isinstance(entries, list):
        raise ValueError('入库流水不完整，已保留上次数据。')
    initialized = db.execute('SELECT 1 FROM journal_baselines WHERE sku=?', (product['sku'],)).fetchone()
    emitted = False
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get('sku') != product['sku']
                or entry.get('warehouse') != '公司大库' or not entry.get('id')):
            raise ValueError('入库流水商品或仓库不匹配。')
        amount = quantity(entry.get('in_qty'))
        timestamp = quantity(entry.get('created_at'))
        if timestamp > current['observed_at'] + 300:
            raise ValueError('入库流水时间晚于当前检查。')
        if entry.get('kind') != '入库单' or not str(entry.get('bill_code', '')).startswith('RK-') or amount <= 0:
            continue
        ident = hashlib.sha256((product['sku']+'|'+str(entry['id'])).encode()).hexdigest()
        inserted = db.execute('INSERT OR IGNORE INTO inbound_seen VALUES (?,?,?)',
                              (ident, product['sku'], timestamp)).rowcount
        if inserted and initialized:
            after = dict(current, inbound=dict(id=ident, bill_code=entry['bill_code'], kind='入库单',
                                              in_qty=amount, created_at=timestamp))
            emit(db, product, 'inbound', old, after, 'ledger_receipt')
            emitted = True
    db.execute('INSERT OR IGNORE INTO journal_baselines VALUES (?)', (product['sku'],))

    return emitted
