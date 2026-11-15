"""Versioned, decimal profit contract shared with the monitor package."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

FORMULA_VERSION = 'profit/v1'
LOSS_CENTS = -30000
CHANGE_CENTS = 10000


def cents(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            return None
        return int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def rounded(value):
    return int(Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def installment_fee(price_cents, term, original_price=None):
    try:
        term = int(term) if str(term) in ('0', '12', '24') else None
    except (TypeError, ValueError):
        term = None
    if term not in (0, 12, 24):
        return None
    rate = {0: Decimal('0'), 12: Decimal('.06'), 24: Decimal('.10')}[term]
    return rounded(Decimal(str(original_price)) * Decimal(100) * rate) if original_price is not None else rounded(Decimal(price_cents) * rate)


def business_snapshot(config):
    """Copy only costing inputs; presentation, credentials and images stay out."""
    price = cents(config.get('price'))
    term = config.get('installment', 0)
    fee = installment_fee(price, term, config.get('price')) if price is not None else None
    rows = []
    for index, part in enumerate(config.get('actualParts', config.get('parts', []))):
        if not (part.get('name') or part.get('goodsId')) or part.get('specialComponent') is True:
            continue
        quantity = part.get('qty')
        try:
            quantity = int(quantity) if str(quantity).isdigit() else None
        except (ValueError, TypeError):
            quantity = None
        if quantity is not None and not 1 <= quantity <= 1000:
            quantity = None
        goods_id = str(part.get('goodsId') or '')
        rows.append(dict(lineId=str(part.get('lineId') or f'{index}:{part.get("slot", "")}'),
                         slot=str(part.get('slot') or ''), name=str(part.get('name') or ''),
                         goodsId=goods_id, qty=quantity, taxCents=cents(part.get('tax')),
                         taxUpdatedAt=config.get('taxUpdatedAt')))
    return dict(shopId=str(config.get('shopId') or ''), productId=str(config.get('productId') or ''),
                product=str(config.get('product') or ''), configId=str(config.get('id') or ''),
                name=str(config.get('name') or ''), revision=config.get('updatedAt'),
                priceCents=price, installment=term, feeCents=fee, parts=rows)


def calculate(snapshot, costs, metric):
    """costs maps goods_id to integer cents; absence never means zero."""
    if metric not in ('erp', 'accounting'):
        raise ValueError('unknown profit metric')
    price, fee = snapshot['priceCents'], snapshot['feeCents']
    if price is None or fee is None:
        return dict(valid=False, reason='到手价或分期无效', profitCents=None, totalCostCents=None)
    rows = snapshot['parts']
    if not rows:
        return dict(valid=False, reason='没有实际成本配件', profitCents=None, totalCostCents=None)
    total, issues = 0, []
    for part in rows:
        quantity = part['qty']
        if quantity is None:
            issues.append(part['slot'] + '数量无效')
            continue
        if metric == 'erp':
            key = part['goodsId']
            unit = costs.get(key) if re.fullmatch(r'[1-9]\d{0,19}', key) else None
        else:
            unit = part['taxCents']
        if type(unit) is not int or unit < 0:
            issues.append(part['slot'] + (' ERP 成本缺失' if metric == 'erp' else '含税价缺失'))
        else:
            total += unit * quantity
    if issues:
        return dict(valid=False, reason='；'.join(issues), profitCents=None, totalCostCents=None)
    profit = (rounded(Decimal(price) * Decimal('.98')) - total - fee if metric == 'erp'
              else price - rounded(Decimal(total) * Decimal('1.04') + Decimal(10000)) - fee)
    return dict(valid=True, reason='', profitCents=profit, totalCostCents=total)


def compare(profit_cents, previous=None, anchor=None, was_loss=False):
    loss = profit_cents < LOSS_CENTS
    delta = profit_cents - anchor if anchor is not None else None
    change = delta is not None and abs(delta) > CHANGE_CENTS
    return dict(loss=loss, change=change, recovered=bool(was_loss and not loss),
                previousDeltaCents=profit_cents - previous if previous is not None else None,
                anchorDeltaCents=delta, reasons=[name for name, hit in [('loss', loss), ('change', change), ('recovered', was_loss and not loss)] if hit])
