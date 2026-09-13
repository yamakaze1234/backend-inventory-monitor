import tempfile
import unittest
from inventory_monitor import InventoryService
from inventory_browser_recovery import BrowserRecovery, ORIGIN


class FakeCli:
    def __init__(self):
        self.calls=[]
        self.page=dict(origin=ORIGIN,path='/login.jsp',client='one',filled=True,button=True)
    def run(self,*args):
        self.calls.append(args)
        if args[0]=='eval': return dict(self.page)
        if args[0]=='find': return {'matches_n':1,'entries':[{'ref':7,'visible':True}]}
        return {}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.s=InventoryService(temp.name);self.cli=FakeCli()
        self.r=BrowserRecovery(self.s,self.cli)
        self.put('enabled',True);self.put('erp_native_enabled',True)
        self.request(100)
    def put(self,k,v):
        with self.s.db() as db:self.s.put(db,k,v)
    def get(self,k):
        with self.s.db() as db:return self.s.get(db,k)
    def request(self,t):
        self.put('erp_native_request',dict(enabled=True,client_id='one',at=t,state='native_ready'))
    def clicks(self):return [c for c in self.cli.calls if c[0]=='click']
    def test_three_bounded_clicks_and_restart_does_not_reset_limit(self):
        for t in (100,105,110,120,130):
            self.request(t);self.r.run_once(t)
        self.assertEqual(len(self.clicks()),3)
        self.assertEqual(self.get('erp_native_state')['status'],'exhausted')
        self.r=BrowserRecovery(self.s,self.cli);self.request(140);self.r.run_once(140)
        self.assertEqual(len(self.clicks()),3)
    def test_desktop_switch_and_stale_request_prevent_browser_access(self):
        self.put('erp_native_enabled',False);self.r.run_once(100)
        self.assertEqual(self.cli.calls,[])
        self.put('erp_native_enabled',True);self.r.run_once(200)
        self.assertEqual(self.cli.calls,[])
    def test_wrong_tab_is_never_clicked_or_navigated(self):
        self.cli.page['origin']='https://www.kdocs.cn';self.r.run_once(100)
        self.assertEqual(self.clicks(),[])
        self.assertEqual(self.cli.calls[-1],('unbind',))
        self.assertEqual(self.get('erp_native_state')['status'],'wrong_tab')
    def test_same_origin_wrong_collector_is_rejected(self):
        self.cli.page['client']='other';self.r.run_once(100)
        self.assertEqual(self.clicks(),[])
    def test_captcha_and_inflight_request_prevent_click(self):
        self.cli.page['captcha']=True;self.r.run_once(100)
        self.assertEqual(self.clicks(),[])
        self.cli.page['captcha']=False;self.cli.page['requesting']=True
        self.request(110);self.r.run_once(110)
        self.assertEqual(self.clicks(),[])
    def test_success_schedules_one_collection_without_changing_page(self):
        self.r.run_once(100)
        self.put('next_check',10000);self.cli.page['path']='/index.htm'
        self.request(110);self.r.run_once(110)
        self.assertEqual(self.get('next_check'),0)
        self.put('next_check',20000);self.request(120);self.r.run_once(120)
        self.assertEqual(self.get('next_check'),20000)
        self.assertEqual(len(self.clicks()),1)
