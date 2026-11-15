"""钉钉订单汇总同步；只以完整运单的真实揽收轨迹删除本程序管理的记录。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from shipping_monitor import CN, date, identifier


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def save_json(path, data):
    # 原子更新同步状态；未知写入保留，不盲目重放创建或删除。
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    # Windows 短暂读取可能占用目标文件；只重试本地原子替换，不重放在线写入。
    for attempt in range(6):
        try:
            pending.replace(path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.2 * (attempt + 1))


def collected_route(response, number):
    """同时校验业务成功、运单身份和实际发生的物流节点。"""
    if response.get('ok') is not True:
        return False, '物流查询失败'
    data = response.get('data', {})
    codes = ('S0000', 'A1000') if response.get('provider') == 'sf-sdk' else ('S0000',)
    if (not isinstance(data, dict) or data.get('errorCode') not in codes
            or (data.get('success') is not True and data.get('success') != 'true')):
        return False, '物流业务结果未确认'
    # SDK 可能返回 JSON 字符串；必须取得可按运单核对的路由列表。
    body = data.get('msgData')
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return False, '物流路由格式未确认'
    rows = body.get('routeResps') if isinstance(body, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return False, '物流路由格式未确认'
    matches = [x for x in rows if x.get('mailNo') == number]
    if len(matches) != 1:
        return False, '运单响应缺失或重复'
    routes = matches[0].get('routes') or []
    if not isinstance(routes, list) or any(not isinstance(node, dict) for node in routes):
        return False, '物流节点格式未确认'
    # 不使用“包含揽收”的模糊匹配，避免把“未揽收/取消揽收”认成已收件。
    accepted = {'已收件', '已揽收', '已揽件', '已收取', '运输中', '运送中', '派送中', '派件中', '已签收'}
    categories = ('', None, '收件', '揽收', '运输', '派送', '签收')
    evidence = []
    for node in routes:
        # 一级可能只写“签收”，二级才给出“已签收”；只接受明确完成的状态值。
        first = node.get('firstStatusName')
        second = node.get('secondaryStatusName')
        state = second if first in categories else first
        if not isinstance(state, str) or state not in accepted:
            continue
        try:
            occurred = date(node.get('acceptTime'))
        except (ValueError, TypeError):
            continue
        if occurred <= datetime.now(CN):
            evidence.append((occurred, state))
    if evidence:
        occurred, state = max(evidence)
        return True, occurred.strftime('%Y-%m-%d %H:%M') + ' ' + state
    return False, '未取得明确揽收节点' if routes else '无物流轨迹，待核实'


class Logistics:
    def __init__(self, root, config):
        self.root, self.config, self.clients = root, config, {}
        self.direct_error = ''

    def close(self):
        for client in self.clients.values():
            client.close()

    def parcels(self, shop, code):
        from batch_match import connect
        if shop not in self.clients:
            self.clients[shop] = connect(shop)
        raw = self.clients[shop].order_detail(code)
        heads = raw.get('data_order')
        if (not isinstance(heads, list) or len(heads) != 1 or identifier(heads[0].get('c_billcode')) != code
                or identifier(heads[0].get('shop_id')) not in ('', shop)):
            raise ValueError('物流订单身份无法核验')
        logs = raw.get('data_order_print_log')
        if not isinstance(logs, list) or not logs:
            return [], False, None
        numbers = set()
        complete = True
        for item in logs:
            value = str(item.get('c_delivery_billcode') or '').strip().upper()
            if not value:
                complete = False
            else:
                numbers.update(x for x in re.split(r'[,，;；\s]+', value) if x)
        # 商品行上的拆包单号也必须覆盖，不能只检查主订单的第一个运单。
        for item in raw.get('data_order_goods', []):
            value = str(item.get('c_platform_delivery_billcode') or '').strip().upper()
            if value:
                numbers.update(x for x in re.split(r'[,，;；\s]+', value) if x)
        # 仅使用真实可见的末四位，不能从尾号已脱敏的手机号中拼出错误校验码。
        phone = re.search(r'([0-9]{4})\s*$', str(heads[0].get('c_receiver_mobile') or heads[0].get('c_receiver_phone') or ''))
        return sorted(numbers), complete and bool(numbers), phone.group(1) if phone else None

    def check(self, shop, code):
        numbers, complete, phone = self.parcels(shop, code)
        result = {'numbers': numbers, 'complete': complete, 'collected': False,
                  'checkedAt': datetime.now(CN).isoformat(), 'evidence': []}
        if not numbers:
            return dict(result, status='尚无运单，保留记录')
        # 密钥留在本机独立文件中，不写进源码、日志或在线表格。
        auth = read_json(self.root / 'shipping-sf-auth.json', {})
        token = str(auth.get('apiKey') or os.environ.get('SHIPPING_SF_API_KEY') or '')
        direct = auth.get('provider') in ('sf-open', 'sf-sdk')
        if not token and not direct:
            return dict(result, status='物流接口未授权，待核实揽收')
        if direct and self.direct_error:
            return dict(result, status='顺丰接口未确认（' + self.direct_error + '），保留记录')
        for number in numbers:
            if not re.fullmatch(r'SF\d{10,18}', number):
                result['evidence'].append({'number': number, 'collected': False, 'status': '非顺丰或运单格式待核实'})
                continue
            payload = {'tracking_number': number}
            if phone:
                payload['check_phone_no'] = phone
            try:
                if direct:
                    from shipping_sf_route import query_route
                    response = query_route(auth, number, phone)
                    ok, status = collected_route(response, number)
                    if not response.get('ok'):
                        status = '顺丰接口未确认（' + response.get('errorCode', 'UNKNOWN') + '），保留记录'
                        # 认证、权限或限流失败后，本轮不对其余订单重复发起相同无效请求。
                        if response.get('errorCode') in ('A1003', 'A1004', 'A1005', 'A1006', 'A1011'):
                            self.direct_error = response['errorCode']
                else:
                    request = urllib.request.Request(self.config['routeUrl'], data=json.dumps(payload).encode(),
                        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
                    with urllib.request.urlopen(request, timeout=35) as response:
                        ok, status = collected_route(json.load(response), number)
            except Exception:
                ok, status = False, '物流查询失败，保留记录'
            result['evidence'].append({'number': number, 'collected': ok, 'status': status})
        result['collected'] = complete and all(x['collected'] for x in result['evidence']) and bool(result['evidence'])
        result['status'] = '全部包裹已确认揽收' if result['collected'] else '；'.join(x['status'] for x in result['evidence'])
        if not complete:
            result['status'] += '；存在未填运单的包裹，保留记录'
        return result


class UnconfirmedTableResponse(RuntimeError):
    """传输回执不可解析；只允许读后核验，不代表写入失败。"""


class Table:
    def __init__(self, config, root):
        self.config, self.root = config, root

    def call(self, args, write=False):
        from group_order_bot import DWS
        command = [DWS, 'aitable', *args, '--base-id', self.config['baseId'], '--table-id', self.config['tableId'],
                   '--profile', self.config['profile'], '--format', 'json']
        if write:
            command.append('--yes')
        # 只读查询遇到瞬时空响应可重查；写入永远只发一次。
        attempts = 1 if write else 3
        for attempt in range(attempts):
            try:
                result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=90,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), cwd=self.root)
                try:
                    value = json.loads(result.stdout)
                    if not isinstance(value, dict): raise ValueError()
                except ValueError:
                    # 不回显 stdout/stderr，避免将连接资料或业务内容写入群消息。
                    raise UnconfirmedTableResponse('钉钉响应不是有效 JSON（退出码 '+str(result.returncode)+
                                                   '，响应长度 '+str(len(result.stdout))+'）；写入状态须回读核验') from None
            except (UnconfirmedTableResponse, subprocess.TimeoutExpired) as error:
                if attempt + 1 < attempts:
                    time.sleep(attempt + 1)
                    continue
                if isinstance(error, subprocess.TimeoutExpired):
                    raise UnconfirmedTableResponse('钉钉接口超时；写入状态须回读核验') from None
                raise
            if result.returncode or value.get('success') is False or value.get('error'):
                raise RuntimeError('钉钉操作未确认，已保留同步状态；请核对在线记录')
            return value

    def records(self, ids=None):
        rows, cursor, seen = [], '', set()
        for _ in range(100):
            args = ['+record-query', '--limit', '100']
            if ids:
                args += ['--record-ids', ','.join(ids)]
            if cursor:
                args += ['--cursor', cursor]
            data = self.call(args)['data']
            rows.extend(data['records'])
            if data.get('hasMore') is False:
                return rows
            cursor = data.get('nextCursor')
            if not cursor or cursor in seen:
                raise ValueError('钉钉分页未完整返回')
            seen.add(cursor)
        raise ValueError('在线记录超出分页上限')

    def write(self, record_id, cells):
        def equal_cell(actual, expected):
            # 数字字段回读为字符串，只在预期值为数字时按数值比对；订单号仍严格按文本核验。
            if expected is None:
                return actual in (None, '')
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                from decimal import Decimal, InvalidOperation
                try:
                    return Decimal(str(actual)) == Decimal(str(expected))
                except (InvalidOperation, ValueError):
                    return False
            return ('' if actual is None else actual) == expected
        def matches(rows):
            # 钉钉读取时省略空文本字段；空字符串与未返回字段等价，零值不能这样处理。
            return len(rows) == 1 and all(
                equal_cell(rows[0].get('cells',{}).get(k), v)
                for k,v in cells.items())
        if record_id:
            # 上轮回执未知时先回读。已符合目标则仅确认，不重放已经成功的修改。
            if matches(self.records([record_id])):
                return record_id
            try:
                self.call(['record', 'update', '--records', json.dumps([{'recordId':record_id,'cells':cells}], ensure_ascii=False)], True)
            except UnconfirmedTableResponse:
                # 已知记录的更新回执丢失时，仅回读同一 ID；下面不再发送写入。
                pass
        else:
            result = self.call(['record', 'create', '--records', json.dumps([{'cells':cells}], ensure_ascii=False)], True)
            ids = result.get('data', {}).get('newRecordIds', [])
            if len(ids) != 1:
                raise ValueError('新增记录ID未确认')
            record_id = ids[0]
        # 在线表写后可能暂时返回旧快照；等待实际字段一致，期间只读、不重复写。
        for attempt in range(6):
            if matches(self.records([record_id])):
                return record_id
            if attempt < 5:
                time.sleep(min(attempt + 1, 3))
        raise ValueError('在线记录回读不一致')

    def delete(self, record_id):
        self.call(['+record-delete', '--record-ids', record_id], True)
        # 在线表可能暂时读到旧快照；只重读同一记录，不重发删除。
        for attempt in range(6):
            if not self.records([record_id]):
                return
            if attempt < 5:
                time.sleep(min(attempt + 1, 3))
        raise ValueError('删除记录未确认')


def cells_for(order, route, checked_at, fields):
    who = order['owner']
    risks = [f"{p['name']}：{p['assessment']}（本单{p['quantity']:g}，现货{p.get('stock','未知')}，可销{p.get('available','未知')}）"
             for p in order.get('parts', []) if p['assessment'] not in ('本轮未见缺口', '需求覆盖不完整，待核实')]
    values = {'订单键':order['shopId']+':'+order['code'], '店铺':order['shop'], 'ERP订单号':order['code'],
        '网店订单号':order['platformNo'], '紧急程度':order['level'], '预计发货时间':order['deadline'][:16].replace('T',' '),
        '负责人':who['name'], '业务员':who['business'], '备注候选':' / '.join(who['remarkNames']),
        '缺货汇总':'\n'.join(risks) or order['stockStatus'], '运单号':'\n'.join(route['numbers']),
        '物流状态':route['status'], '揽收核验':'已确认全部包裹揽收' if route['collected'] else '未确认，保留记录',
        '退款状态':'有退款标记，需核实' if order.get('refund') else '无退款标记',
        '巡检时间':checked_at[:19].replace('T',' '), '数据异常':order.get('detailError','')}
    return {fields[name]:str(value) for name,value in values.items()}


def sync(report, root, table_factory=Table, logistics_factory=Logistics):
    """同步当前紧急单和已跟踪旧单；从待发列表消失不代表已揽收。"""
    root = Path(root)
    cfg = read_json(root/'shipping-online.json', {})
    if not cfg.get('enabled'):
        return {'enabled':False}
    from patrol_service import single_instance
    with single_instance(root/'shipping-online.lock') as acquired:
        if not acquired:
            return {'enabled':True, 'complete':False, 'errors':['另一轮同步正在进行']}
        table, logistics = table_factory(cfg, root), logistics_factory(root, cfg)
        state_path = root/'shipping-online-state.json'
        state = read_json(state_path, {'orders':{}, 'pending':{}})
        result = {'enabled':True, 'complete':True, 'updated':0, 'deleted':0, 'retained':0, 'errors':[], 'url':cfg['url']}
        try:
            remote = table.records()
            by_key = {}
            for row in remote:
                key = row.get('cells', {}).get(cfg['fields']['订单键'])
                if not key:
                    continue
                if key in by_key:
                    raise ValueError('在线表出现重复订单键，停止同步')
                by_key[key] = row
            current = {o['shopId']+':'+o['code']:o for o in report.get('orders', []) if o['level'] in ('紧急','已超时')}
            # 本轮列表中消失的订单继续核对物流，不因匹配时间窗口或ERP状态变化删除。
            for key,saved in state['orders'].items():
                if key in by_key and key not in current:
                    old = dict(saved['order'], detailError='本轮不在紧急待发列表；保留上次业务信息并继续核验物流')
                    current[key] = old
            # 回执未知的删除只做回读核对，仍能看到记录时留待核验，绝不重放或覆盖。
            for key,action in list(state['pending'].items()):
                if action != 'delete':
                    continue
                current.pop(key,None)
                saved = state['orders'].get(key)
                record_id = saved.get('recordId') if saved else None
                if not record_id or key in by_key or any(row['recordId']==record_id for row in remote) or table.records([record_id]):
                    result['errors'].append(key+'：上次删除未确认，未重发')
                    continue
                with (root/'shipping-collection-audit.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps({'at':datetime.now(CN).isoformat(),'key':key,'recordId':record_id,'action':'delete_verified'},ensure_ascii=False)+'\n')
                state['orders'].pop(key,None)
                state['pending'].pop(key,None)
                save_json(state_path,state)
            for key,order in current.items():
                try:
                    record = by_key.get(key)
                    saved = state['orders'].get(key)
                    if saved and any(r['recordId'] == saved['recordId'] and
                                     r.get('cells',{}).get(cfg['fields']['订单键']) != key for r in remote):
                        raise ValueError('已管理在线行的订单键被修改，停止处理该单')
                    if key in state['pending'] and not record:
                        raise ValueError('上次写入结果未知，须核验后处理，未重复创建')
                    route = logistics.check(order['shopId'], order['code'])
                    if route['collected']:
                        if record and key in state['orders']:
                            if record['recordId'] != saved['recordId']:
                                raise ValueError('在线记录与本机管理登记不一致，保留记录')
                            # 删除前再次确认订单所有包裹与刚才核验的集合一致。
                            numbers, complete, _ = logistics.parcels(order['shopId'], order['code'])
                            if not complete or numbers != route['numbers']:
                                raise ValueError('删除前运单集合发生变化，保留记录')
                            fresh = table.records([record['recordId']])
                            if (len(fresh) != 1 or fresh[0]['recordId'] != record['recordId']
                                    or fresh[0]['cells'].get(cfg['fields']['订单键']) != key):
                                raise ValueError('删除前在线订单身份变化，保留记录')
                            # 删除前保存订单内容及真实轨迹，线上删行不删除本机审计证据。
                            with (root/'shipping-collection-audit.jsonl').open('a',encoding='utf-8') as f:
                                f.write(json.dumps({'at':datetime.now(CN).isoformat(),'key':key,'record':fresh[0],'route':route,'action':'delete_requested'},ensure_ascii=False)+'\n')
                            state['pending'][key] = 'delete'
                            save_json(state_path,state)
                            table.delete(record['recordId'])
                            result['deleted'] += 1
                            with (root/'shipping-collection-audit.jsonl').open('a',encoding='utf-8') as f:
                                f.write(json.dumps({'at':datetime.now(CN).isoformat(),'key':key,'recordId':record['recordId'],'action':'delete_verified'},ensure_ascii=False)+'\n')
                            state['orders'].pop(key,None)
                            state['pending'].pop(key,None)
                            save_json(state_path,state)
                        elif record:
                            # 非本程序建立/接管的行不能直接删除。
                            result['retained'] += 1
                        continue
                    cells = cells_for(order,route,report.get('checkedAt',datetime.now(CN).isoformat()),cfg['fields'])
                    state['pending'][key] = 'update' if record else 'create'
                    save_json(state_path,state)
                    record_id = table.write(record['recordId'] if record else None,cells)
                    state['orders'][key] = {'recordId':record_id,'order':order,'route':route}
                    state['pending'].pop(key,None)
                    save_json(state_path,state)
                    result['updated'] += 1
                except Exception as error:
                    from group_order_bot import safe_error
                    result['errors'].append(key+'：'+safe_error(error))
            result['complete'] = not result['errors']
        except Exception as error:
            from group_order_bot import safe_error
            result['errors'].append(safe_error(error)); result['complete'] = False
        finally:
            logistics.close()
        save_json(root/'shipping-online-last-sync.json',result)
        return result
