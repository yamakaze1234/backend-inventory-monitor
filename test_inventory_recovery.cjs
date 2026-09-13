const test=require('node:test');
const assert=require('node:assert/strict');
const {loginDecision,browserFilled,refreshRecoveryState}=require('./inventory-erp.user.js');
const ready={enabled:true,captcha:false,autofilled:true,attempts:0,lastAttempt:0,now:100000};
const vm=require('node:vm');
const fs=require('node:fs');
test('recovery refreshes once across document reload and preserves persistent enablement',()=>{
  const started=refreshRecoveryState({enabled:true,pending:false,attempts:0});
  assert.equal(started.enabled,true);
  assert.equal(started.refreshed,true);
  const persisted=JSON.parse(JSON.stringify(started));
  assert.equal(refreshRecoveryState(persisted),null);
  assert.equal(refreshRecoveryState({...persisted,attempts:2,lastAttempt:123}),null);
  assert.equal(refreshRecoveryState({...persisted,pending:false}).refreshed,true);
  assert.equal(refreshRecoveryState({enabled:false,pending:false}),null);
});
test('migration from earlier pending recovery refreshes without resetting retry limits',()=>{
  const state=refreshRecoveryState({enabled:true,pending:true,attempts:3,lastAttempt:123});
  assert.equal(state.attempts,3);
  assert.equal(state.lastAttempt,123);
});
function recoveryPage({enabled=true, delayed=false}={}) {
  let now=100000,value='',options=delayed?[]:[{id:2,c_name:'公司大库'}];
  const calls=[],timers=[];
  let frameOpen=true,menuQueries=0;
  const search={textContent:'搜索',getClientRects:()=>[{}],click:()=>calls.push('search')};
  const toolbar={querySelectorAll:()=>[search]};
  const control={el:{getClientRects:()=>[{}],closest:()=>toolbar},valueField:'id',textField:'c_name',
    getData:()=>options,getValue:()=>value,getText:()=>value==='2'?'公司大库':'',
    setValue:v=>{value=v;calls.push('warehouse');},fire:()=>{}};
  const stockDoc={querySelectorAll:()=>[]};
  const stock={location:{pathname:'/pages/stock/stock_list.jsp'},document:stockDoc,mini:{get:id=>id==='depot_id'?control:null}};
  const account={getClientRects:()=>[{}],innerText:'测试账号'};
  const document={body:{appendChild:()=>{}},createElement:()=>({style:{},setAttribute:()=>{},addEventListener:()=>{}}),
    querySelector:()=>({querySelectorAll:()=>[account]}),
    querySelectorAll:s=>{
      if(s==='iframe') return frameOpen?[{contentWindow:stock}]:[];
      if(s.includes('dropdown-item')) menuQueries++;
      return [];
    }};
  const window={document}; window.top=window;window.self=window;
  window.location={pathname:'/index.htm',origin:'https://cqzs.3cerp.com'};
  const context={window,unsafeWindow:window,document,location:window.location,crypto:{randomUUID:()=> 'test'},
    Date:class extends Date {static now(){return now;}},
    GM_getValue:k=>k==='inventory_connection'?{depot:'2',account:'测试账号'}:{enabled,pending:false},
    GM_setValue:()=>{},GM_registerMenuCommand:()=>{},
    GM_xmlhttpRequest:o=>{if(o.url.endsWith('/recovery-config')) o.onload({status:200,responseText:JSON.stringify({enabled})});else {calls.push('poll');o.onload({status:200,responseText:'{"check_id":null}'});}},
    setInterval:f=>timers.push(f),clearInterval:()=>{}};
  vm.runInNewContext(fs.readFileSync(require.resolve('./inventory-erp.user.js'),'utf8'),context);
  return {ready:async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve();},calls,menuQueries:()=>menuQueries,closeStock:()=>frameOpen=false,load:()=>options=[{id:2,c_name:'公司大库'}],tick:async()=>{now+=2000;await timers[0]();}};
}
test('existing inventory frame never activates its menu or reopens a user-closed tab',async()=>{
  const page=recoveryPage(); await page.ready();
  await page.tick(); await page.tick();
  assert.equal(page.menuQueries(),0);
  page.closeStock();
  for(let n=0;n<12;n++) await page.tick();
  assert.equal(page.menuQueries(),0);
});
test('refresh with cleared pending restores warehouse, searches, then reconnects without prompting',async()=>{
  const page=recoveryPage(); await page.ready();
  assert.deepEqual(page.calls,['warehouse']);
  await page.tick(); assert.deepEqual(page.calls,['warehouse','search']);
  await page.tick(); assert.deepEqual(page.calls,['warehouse','search','poll']);
  for(let n=0;n<12;n++) await page.tick();
  assert.equal(page.calls.filter(c=>c==='search').length,1);
});
test('waits for warehouse options and resumes without a manual click',async()=>{
  const page=recoveryPage({delayed:true}); await page.ready();
  await page.tick(); assert.deepEqual(page.calls,[]);
  page.load(); await page.tick(); await page.tick(); await page.tick();
  assert.deepEqual(page.calls,['warehouse','search','poll']);
});
test('disabled recovery does not select or search',async()=>{
  const page=recoveryPage({enabled:false}); await page.ready(); await page.tick();
  assert.deepEqual(page.calls,[]);
});
test('requires enablement and browser autofill, stops for captcha',()=>{
  assert.equal(loginDecision(ready),'submit');
  assert.equal(loginDecision({...ready,enabled:false}),'disabled');
  assert.equal(loginDecision({...ready,autofilled:false}),'autofill');
  assert.equal(loginDecision({...ready,captcha:true}),'captcha');
});
test('waits ten seconds after document load even when autofill already looks ready',()=>{
  const state={...ready,readyAt:110000};
  assert.equal(loginDecision(state),'settling');
  assert.equal(loginDecision({...state,now:109999}),'settling');
  assert.equal(loginDecision({...state,now:110000}),'submit');
  assert.equal(loginDecision({...state,now:110000,autofilled:false}),'autofill');
  assert.equal(loginDecision({...state,captcha:true}),'captcha');
  assert.equal(loginDecision({...state,enabled:false}),'disabled');
});
test('waits for the final login outcome before declaring attempts exhausted',()=>{
  assert.equal(loginDecision({...ready,attempts:3,lastAttempt:96000}),'cooldown');
  assert.equal(loginDecision({...ready,attempts:3,lastAttempt:40000}),'exhausted');
});
test('repeats at five second intervals up to three clicks and never overlaps a request',()=>{
  let state={...ready};
  for(let n=0;n<3;n++) {
    assert.equal(loginDecision(state),'submit');
    state={...state,attempts:n+1,lastAttempt:state.now};
    assert.equal(loginDecision({...state,now:state.now+4999}),'cooldown');
    state.now+=5000;
  }
  assert.equal(loginDecision(state),'exhausted');
  assert.equal(loginDecision({...ready,requesting:true}),'cooldown');
  assert.equal(loginDecision({...ready,rejected:true}),'rejected');
  assert.equal(loginDecision({...ready,captcha:true}),'captcha');
});
test('autofill check never reads credential values and ignores hidden controls',()=>{
  const input={get value(){throw Error('must not access credentials')},getClientRects:()=>[{}],matches:s=>s===':-webkit-autofill'};
  assert.equal(browserFilled(input),true);
  assert.equal(browserFilled({...Object.getOwnPropertyDescriptors(input),getClientRects:()=>[]}),false);
  assert.equal(browserFilled({getClientRects:()=>[{}],matches:()=>false}),false);
});
