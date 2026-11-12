"""顺丰只读生产接口：签名、返回边界与安全地址核验。"""
import base64
import hashlib
import io
import json
from pathlib import Path
import runpy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs


class SFRouteTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'integrations/order_backend/matching/shipping_sf_route.py'
        self.module = runpy.run_path(str(path))
        self.auth = dict(environment='production', partnerId='test-partner', checkWord='test-secret')

    def test_signed_query_uses_official_host_and_only_route_service(self):
        request = self.module['build_request'](self.auth, 'SF1234567890123', '1234', timestamp=123, request_id='test-id')
        fields = {k:v[0] for k,v in parse_qs(request.data.decode()).items()}
        self.assertEqual(request.full_url, 'https://bspgw.sf-express.com/std/service')
        self.assertEqual(fields['serviceCode'], 'EXP_RECE_SEARCH_ROUTES')
        self.assertEqual(json.loads(fields['msgData'])['checkPhoneNo'], '1234')
        expected = base64.b64encode(hashlib.md5((fields['msgData']+'123test-secret').encode()).digest()).decode()
        self.assertEqual(fields['msgDigest'], expected)
        self.assertNotIn('test-secret', request.data.decode())
        self.assertNotIn('Authorization', request.headers)

    def test_sandbox_never_supplies_production_collection_evidence(self):
        with self.assertRaises(ValueError):
            self.module['build_request'](dict(self.auth, environment='sandbox'), 'SF1234567890123')

    def test_denied_permission_does_not_expose_upstream_message(self):
        response = dict(apiResultCode='A1004', apiErrorMsg='secret data must not be returned')
        result = self.module['query_route'](self.auth, 'SF1234567890123',
            opener=lambda *a,**kw: io.BytesIO(json.dumps(response).encode()))
        self.assertEqual(result, dict(ok=False, errorCode='A1004'))

    def test_success_decodes_nested_json_without_promoting_business_failure(self):
        business = dict(success=False, errorCode='8181', msgData=None)
        response = dict(apiResultCode='A1000', apiResultData=json.dumps(business))
        result = self.module['query_route'](self.auth, 'SF1234567890123',
            opener=lambda *a,**kw: io.BytesIO(json.dumps(response).encode()))
        self.assertFalse(result['data']['success'])

    def test_phone_must_be_exactly_four_digits(self):
        for phone in ('12**', '12345', '123'):
            with self.assertRaises(ValueError):
                self.module['build_request'](self.auth, 'SF1234567890123', phone)

    def test_sdk_permission_error_is_sanitized_and_client_closed(self):
        class SfError(Exception):
            pass
        client = Mock()
        client.查询路由.side_effect = SfError('A1004: private upstream context')
        with patch.dict('sys.modules', {'sf':SimpleNamespace(Sf=Mock(return_value=client)),
                                      'sf.client':SimpleNamespace(SfError=SfError)}):
            result = self.module['query_route'](dict(self.auth, provider='sf-sdk'), 'SF1234567890123')
        self.assertEqual(result, dict(ok=False, errorCode='A1004'))
        client.close.assert_called_once()

    def test_sdk_route_requires_matching_waybill_and_completed_status(self):
        # 复现仓库示例：一级是“签收”分类，二级才给出完成状态。
        path = Path(__file__).resolve().parents[1] / 'integrations/order_backend/matching/shipping_online.py'
        monitor = SimpleNamespace(CN=timezone.utc, date=datetime.fromisoformat, identifier=str)
        with patch.dict('sys.modules', shipping_monitor=monitor):
            collected_route = runpy.run_path(str(path))['collected_route']
        number = 'SF1234567890123'
        node = dict(firstStatusName='签收', secondaryStatusName='已签收', acceptTime='2020-01-01T00:00:00+00:00')
        row = dict(mailNo=number, routes=[node])
        client = Mock()
        client.查询路由.return_value = dict(success='true', errorCode='A1000', msgData=json.dumps(dict(routeResps=[row])))
        with patch.dict('sys.modules', {'sf':SimpleNamespace(Sf=Mock(return_value=client)),
                                      'sf.client':SimpleNamespace(SfError=RuntimeError)}):
            response = self.module['query_route'](dict(self.auth, provider='sf-sdk'), number)
        self.assertTrue(collected_route(response, number)[0])
        self.assertFalse(collected_route(response, 'SF1234567890124')[0])
        self.assertFalse(collected_route(dict(response, provider=None), number)[0])
        for rows in ([], [row, row], [dict(row, routes=[])],
                     [dict(row, routes=[dict(node, firstStatusName='待揽收')])],
                     [dict(row, routes=[dict(node, acceptTime='2099-01-01T00:00:00+00:00')])]):
            with self.subTest(rows=rows):
                data = dict(response['data'], msgData=dict(routeResps=rows))
                self.assertFalse(collected_route(dict(response, data=data), number)[0])
