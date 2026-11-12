"""顺丰生产路由只读查询：本机校验码签名，不记录请求或凭据。"""
import base64
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import uuid

PRODUCTION_URL = 'https://bspgw.sf-express.com/std/service'


def build_request(auth, number, phone=None, *, timestamp=None, request_id=None):
    # 凭据只能用于固定的官方生产地址，沙箱配置不能作为真实揽收证据。
    if auth.get('environment') != 'production' or not auth.get('partnerId') or not auth.get('checkWord'):
        raise ValueError('顺丰生产认证配置不完整')
    if not re.fullmatch(r'SF\d{10,18}', number):
        raise ValueError('顺丰运单格式待核实')
    data = dict(language='zh-CN', trackingType=1, trackingNumber=[number], methodType=1)
    if phone:
        if not re.fullmatch(r'\d{4}', phone):
            raise ValueError('手机号后四位格式不正确')
        data['checkPhoneNo'] = phone
    message = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    stamp = str(int(time.time() * 1000)) if timestamp is None else str(timestamp)
    # 官方简易 MD5：业务 JSON + 毫秒时间戳 + 生产校验码，MD5 后 Base64。
    digest = base64.b64encode(hashlib.md5((message + stamp + auth['checkWord']).encode('utf-8')).digest()).decode('ascii')
    fields = dict(partnerID=auth['partnerId'], requestID=request_id or str(uuid.uuid4()),
                  serviceCode='EXP_RECE_SEARCH_ROUTES', timestamp=stamp, msgData=message, msgDigest=digest)
    return urllib.request.Request(PRODUCTION_URL, data=urllib.parse.urlencode(fields).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'}, method='POST')


def query_route(auth, number, phone=None, *, opener=urllib.request.urlopen):
    request = build_request(auth, number, phone)
    if auth.get('provider') == 'sf-sdk':
        # 已安装的独立 sf 包提供同一只读查询；仍先校验生产配置、单号及手机号格式。
        from sf import Sf
        from sf.client import SfError
        client = Sf(auth['partnerId'], auth['checkWord'])
        try:
            data = client.查询路由(number, checkPhoneNo=phone)
            return dict(ok=True, data=data) if isinstance(data, dict) else dict(ok=False, errorCode='INVALID_RESPONSE')
        except SfError as error:
            code = re.match(r'^(A\d{4})\b', str(error))
            return dict(ok=False, errorCode=code[1] if code else 'SDK_ERROR')
        finally:
            client.close()
    with opener(request, timeout=35) as response:
        value = json.load(response)
    # 仅保留错误码，避免把上游异常中可能回显的顾客编码、签名或手机号带进日志。
    code = str(value.get('apiResultCode', ''))
    if code != 'A1000':
        return dict(ok=False, errorCode=code if re.fullmatch(r'[A-Z0-9]{1,20}', code) else 'UNKNOWN')
    data = value.get('apiResultData')
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        return dict(ok=False, errorCode='INVALID_RESPONSE')
    return dict(ok=True, data=data)
