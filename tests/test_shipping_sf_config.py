"""顺丰认证界面与后端共享配置的回归验证。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import shipping_sf_config as sf


class ShippingSFConfigTests(unittest.TestCase):
    def test_encrypted_roundtrip_blank_preserves_and_no_secret_in_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            values=dict(partnerId='demo-partner', checkWord='demo-secret', provider='sf-open')
            sf.save(directory, values)
            raw=(Path(directory)/'shipping-sf-auth.json').read_text()
            self.assertNotIn('demo-secret', raw)
            self.assertEqual(sf.load(directory)['checkWord'], 'demo-secret')
            self.assertNotIn('checkWord', sf.summary(directory))
            sf.save(directory, dict(values, checkWord=''))
            self.assertEqual(sf.load(directory)['checkWord'], 'demo-secret')
            with self.assertRaises(ValueError):
                sf.save(directory, dict(values, partnerId='other', checkWord=''))

    def test_legacy_plaintext_migrates_on_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'shipping-sf-auth.json'
            path.write_text(json.dumps(dict(provider='sf-sdk', environment='production', partnerId='demo', checkWord='old-secret')))
            sf.save(directory, dict(provider='sf-sdk', partnerId='demo', checkWord=''))
            self.assertNotIn('old-secret', path.read_text())
            self.assertEqual(sf.load(directory)['provider'], 'sf-sdk')
            self.assertEqual(sf.load(directory)['checkWord'], 'old-secret')

    def test_invalid_input_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            values=dict(partnerId='demo',checkWord='secret',provider='sf-open')
            sf.save(directory, values)
            path=Path(directory)/'shipping-sf-auth.json'
            before=path.read_bytes()
            for change in [dict(checkWord='bad 复制'),dict(provider='sandbox'),dict(partnerId='')]:
                with self.assertRaises(ValueError): sf.save(directory, dict(values, **change))
                self.assertEqual(path.read_bytes(), before)

    def test_old_backend_cannot_receive_encrypted_credentials(self):
        from inventory_business_actions import shipping_sf_auth_save
        bridge=SimpleNamespace(external=lambda:None,order_bot=SimpleNamespace(configuration=lambda:dict(configured=True,state_root='unused',shippingBackendCurrent=False)))
        with patch('shipping_sf_config.save') as save:
            with self.assertRaises(ValueError): shipping_sf_auth_save(bridge,{})
            save.assert_not_called()

    def test_backend_copy_matches(self):
        root=Path(__file__).resolve().parents[1]
        self.assertEqual((root/'app/src/shipping_sf_config.py').read_bytes(),(root/'integrations/order_backend/matching/shipping_sf_config.py').read_bytes())

    def test_new_user_can_replace_unreadable_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'shipping-sf-auth.json'
            path.write_text('{"checkWordEncrypted":"broken"}')
            with self.assertRaises(ValueError): sf.save(directory,dict(partnerId='demo',checkWord=''))
            sf.save(directory,dict(partnerId='demo',checkWord='new-secret'))
            self.assertEqual(sf.load(directory)['checkWord'],'new-secret')

    def test_shipping_page_exposes_password_field_without_secret(self):
        from shipping_business import view
        with tempfile.TemporaryDirectory() as directory:
            sf.save(directory,dict(partnerId='demo',checkWord='hidden-secret'))
            state=dict(configured=True,state_root=directory)
            bridge=SimpleNamespace(service=SimpleNamespace(),order_bot=SimpleNamespace(configuration=lambda:state))
            with patch('shipping_business.notification_state',return_value=state),patch('shipping_business.shipping_plan',return_value=dict(forms=[])):
                result=view(bridge,{})
            section=next(s for s in result['sections'] if s.get('id')=='sf-auth')
            field=next(f for f in section['forms'][0]['fields'] if f['key']=='checkWord')
            self.assertEqual(field['kind'],'password')
            self.assertNotIn('hidden-secret',json.dumps(result))
