"""在 Python 3.14 订单后端核对单号、店铺及 tiny_tools 履约记录。"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from importlib import import_module
import re

CN = timezone(timedelta(hours=8))
DETAIL = 'pages/net/searchNetOrderDetailInfo.htm'
OUTBOUND = 'pages/sales/searchSalesDetail.htm'
TRACKING = 'pages/sales/searchAllSalesList.htm'


def identifier(value):
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ''


def web_numbers(value):
    return [s for s in re.split(r'[,，;\s]+', identifier(value)) if s]


class ReadSession:
    def __init__(self, client):
        self.client = client
        self.cache = {}
        self.verified = {}

    def get_api(self, api):
        # 已关联订单先按原始详情核实店铺，SDK 回读只能使用同一份已核实的详情。
        endpoint, params = api.base_url, api.params()
        if endpoint == DETAIL:
            return api.transform_data(self.verified[params['cBillcode']])
        key = (endpoint, repr(sorted(params.items())))
        if key in self.cache:
            return self.cache[key]
        if endpoint == OUTBOUND:
            raw = self.client._request(endpoint, form=params, read_only=True)
            data = raw.get('data')
            if not isinstance(data, dict) or raw.get('result') is False or ('code' in raw and str(raw['code']) != '0'):
                raise ValueError('ERP 出库详情不可读')
        elif endpoint == TRACKING:
            import pandas as pd
            data = pd.DataFrame(self.client._paged(endpoint, params), columns=list(api.name_map())).rename(columns=api.name_map())
        else:
            raise ValueError('不支持的订单查询端点')
        self.cache[key] = data
        return data


def query_verified_orders(number_type, number, shops, *, connect=None, sdk=None, audit_api=None):
    # 延迟导入以保持旧后台可运行；试点环境必须核实包含 PR #5 的依赖提交。
    if connect is None:
        from batch_match import connect
    if sdk is None:
        sdk = import_module('tiny_tools.erp.core.网店订单操作日志查询')
    if audit_api is None:
        from tiny_tools.erp import Api_网店审单 as audit_api
    scope = set(shops)
    if not scope or number_type not in ('网店单号', 'NO单号', '出库单号', '物流单号'):
        return dict(status='unverified', orders=[])
    web = number_type == '网店单号'
    resolved = {}
    if web:
        for shop in shops:
            with closing(connect(shop)) as client:
                query = audit_api(datetime(2000, 1, 1), datetime.now(CN).replace(tzinfo=None),
                                  b_dispose=0, b_match_goods=0, shopId=[shop])
                params = query.params()
                params.update(search_billcode_key='platformNo', search_billcode_filter=number)
                for row in client._paged(query.base_url, params):
                    if identifier(row.get('shop_id')) != shop or 'c_platform_billcode' not in row:
                        return dict(status='unverified', orders=[])
                    if number in web_numbers(row['c_platform_billcode']):
                        code = identifier(row.get('c_billcode'))
                        if not code.startswith('NO-'):
                            return dict(status='unverified', orders=[])
                        resolved[(shop, code)] = True
        if len(resolved) > 1:
            return dict(status='ambiguous', orders=[])
        if not resolved:
            return dict(status='ok', orders=[])
    primary = next(iter(resolved))[0] if resolved else shops[0]
    with closing(connect(primary)) as client:
        session = ReadSession(client)
        if web:
            codes = [next(iter(resolved))[1]]
            args = {'NO单号': codes[0]}
        else:
            args = {number_type: number}
            no, ck, tracking = (args.get(key, '') for key in ('NO单号', '出库单号', '物流单号'))
            try:
                codes, _, _ = sdk._关联单号(session, no, ck, tracking)
            except ValueError as error:
                # 只识别该固定版本明确的“未找到”；其余错误不能伪装为查无订单。
                if str(error) in (f'未找到出库单：{ck}', f'未找到物流单号关联的出库单：{tracking}'):
                    return dict(status='ok', orders=[])
                if str(error) == '出库单未关联 NO 订单，无法查询网店订单状态':
                    return dict(status='unverified', orders=[])
                raise
        if not codes or len(codes) > 50 or len(set(codes)) != len(codes):
            return dict(status='unverified', orders=[])
        orders = []
        for code in codes:
            raw = client.order_detail(code)
            heads = raw.get('data_order')
            if heads == [] and number_type == 'NO单号':
                return dict(status='ok', orders=[])
            if not isinstance(heads, list) or len(heads) != 1:
                return dict(status='unverified', orders=[])
            head, = heads
            shop, actual = (identifier(head.get(k)) for k in ('shop_id', 'c_billcode'))
            if actual != code or shop not in scope:
                return dict(status='unverified', orders=[])
            numbers = web_numbers(head.get('c_platform_billcode'))
            if web and (number not in numbers or (shop, code) not in resolved):
                return dict(status='unverified', orders=[])
            session.verified[code] = raw
            orders.append(dict(erp_no=code, shop_id=shop, web_order_numbers=numbers, events=[]))
        # 全部关联订单均核对后才读取登记；一次查询保留 SDK 的共同关联与重复步骤。
        events = sdk.网店订单操作日志查询(session, **args)
        if not isinstance(events, list):
            raise ValueError('订单操作日志格式无效')
        orders[0]['events'] = [dict(step=r['步骤'], operator=r['操作人'], occurred_at=r['时间'],
                                    reference_number=r['对应单号']) for r in events]
    return dict(status='ok', orders=orders)
