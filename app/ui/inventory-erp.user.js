// ==UserScript==
// @name         公司大库重点产品采集
// @namespace    local.inventory.monitor
// @version      0.4.0
// @description  为本机钉钉货源监控提供只读库存快照，不修改 ERP 数据
// @match        https://cqzs.3cerp.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
(function () {
  'use strict';

  async function collectPages(fetchPage) {
    let rows=[], expected=null;
    for (let page=0; ; page++) {
      const result=await fetchPage(page);
      if (!result || result.result === false || result.success === false || (result.st !== undefined && result.st !== 0))
        throw new Error('ERP 未返回有效库存');
      const data=result.data || result.rows || result.list;
      const total=Number(result.total);
      if (!Array.isArray(data) || result.total === undefined || !Number.isInteger(total) || total<0)
        throw new Error('库存分页数据不完整');
      if (expected===null) expected=total;
      if (total!==expected) throw new Error('分页期间库存总数发生变化，请重试');
      rows.push(...data);
      if (rows.length===expected) return rows;
      if (!data.length || rows.length>expected) throw new Error('库存分页数量不一致');
    }
  }

  function number(value, context='库存数量') {
    const reason=value===null?'null':value===undefined?'字段缺失':
      typeof value==='string' && !value.trim()?'空白':
      !['number','string'].includes(typeof value) || !Number.isFinite(Number(value))?'非数字':null;
    if (reason) throw new Error(context+'：'+reason+'，已停止本次采集');
    return Number(value);
  }

  function normalizeRow(row) {
    const sku=String(row.b_c_sku || '').trim();
    if (!sku || sku==='0') throw new Error('商品编码缺失');
    const goodsId=String(row.goods_id || '');
    const label=/^\d{1,20}$/.test(goodsId)?'商品编号'+goodsId:'商品编号未知';
    return {sku, goods_id:goodsId, name:String(row.b_c_name || ''), able:number(row.n_stock_able,label+' 可用库存(n_stock_able)'),
      purchase:row.n_stock_purchase===null?0:number(row.n_stock_purchase,label+' 待入数量(n_stock_purchase)'),
      stock:number(row.n_stock,label+' 实际库存(n_stock)')};
  }

  function normalizeCatalogRow(row) {
    if (row.n_stock_able !== null) return normalizeRow(row);
    // Validate identity and other fields before isolating ERP's explicit null.
    const valid=normalizeRow({...row,n_stock_able:0});
    return {...valid,able:null,stock_unknown:true};
  }

  function normalizeWarehouseResponse(result, goodsId) {
    goodsId=String(goodsId);
    if (!/^[1-9]\d{0,19}$/.test(goodsId) || !result || result.result!==true ||
        (result.code!==undefined && result.code!==0) || (result.st!==undefined && result.st!==0) ||
        result.success===false || !Array.isArray(result.data) || result.data.length>500)
      throw new Error('分库查询未返回完整数据');
    // Native detail popup has no pager. Its `total` is always 0 even with rows.
    // The stock-record `id` is NOT the depot identity; use depot_id exclusively.
    const seen=new Set();
    const depots=result.data.map(row=>{
      const id=String(row.depot_id || ''), name=String(row.depot_name || '').trim();
      if (String(row.goods_id)!==goodsId || !/^[1-9]\d{0,19}$/.test(id) || seen.has(id) || !name || name.length>100)
        throw new Error('分库商品或仓库身份不匹配');
      seen.add(id);
      const optional=(value,field)=>value===null || value===undefined?null:number(value,'分库 '+field);
      return {id,name,stock:optional(row.n_stock,'库存数'),able:optional(row.n_able,'可销数'),purchase:optional(row.n_purchase,'待入数')};
    });
    return {goods_id:goodsId,complete:true,depots};
  }

  function normalizeJournal(row, sku, occurrence=0) {
    const inQty=row.n_in===null?0:number(row.n_in,'入库流水 入库数量(n_in)');
    const created=Date.parse(String(row.create_time).replace(' ','T')+'+08:00')/1000;
    if (!Number.isFinite(created) || row.c_name!=='公司大库') throw new Error('入库流水时间或仓库不匹配');
    return {id:JSON.stringify([sku,row.c_name,row.create_time,row.c_billcode,row.c_type,occurrence]),
      sku,warehouse:row.c_name,bill_code:String(row.c_billcode || ''),kind:String(row.c_type || ''),in_qty:inQty,created_at:created};
  }

  function loginDecision({enabled, captcha, autofilled, attempts, lastAttempt, now, readyAt=0, rejected=false, requesting=false}) {
    if (!enabled) return 'disabled';
    if (captcha) return 'captcha';
    if (rejected) return 'rejected';
    if (now<readyAt) return 'settling';
    if (requesting || (lastAttempt && now-lastAttempt<5000)) return 'cooldown';
    if (attempts>=3) return 'exhausted';
    if (!autofilled) return 'autofill';
    return 'submit';
  }

  function browserFilled(element) {
    if (!element || !element.getClientRects().length) return false;
    // Inspect only browser autofill state, never access password/username values.
    for (const selector of [':autofill', ':-webkit-autofill']) {
      try { if (element.matches(selector)) return true; } catch (_) {}
    }
    return false;
  }

  function refreshRecoveryState(state) {
    if (!state.enabled || (state.pending && state.refreshed)) return null;
    return {...state,pending:true,refreshed:true,
      attempts:state.pending?(state.attempts||0):0,
      lastAttempt:state.pending?(state.lastAttempt||0):0};
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports={collectPages, normalizeRow, normalizeCatalogRow, normalizeJournal, normalizeWarehouseResponse, loginDecision, browserFilled, refreshRecoveryState};
    return;
  }
  if (window.top!==window.self) return;
  if (window.__inventoryMonitorInstance) return;
  const pageWindow=typeof unsafeWindow==='undefined'?window:unsafeWindow;
  const clientId=crypto.randomUUID();
  let busy=false, nextPoll=0;
  const badge=document.createElement('button');
  badge.setAttribute('data-inventory-client',clientId);
  badge.textContent='库存采集：未连接';
  badge.style.cssText='position:fixed;bottom:10px;right:18px;z-index:99999;background:#101C30;color:white;border:0;padding:9px 14px;font:13px Microsoft YaHei;cursor:pointer';
  badge.title='点击配置；数据只发送到本机库存监控程序';
  document.body.appendChild(badge);
  const recoveryKey='inventory_auto_recovery';
  const clickedMenus=new Set();
  const searchedStockPages=new WeakSet();
  let stockPageSeen=false;
  let loginBusy=false;
  // Browser-managed password fill can lag behind document-idle after refresh.
  const loginReadyAt=Date.now()+10000;
  let lastRecoveryReport=0;
  let nativeStatus='waiting';
  async function syncRecoverySetting(config) {
    const setting=await local('/recovery-config',{client_id:clientId,
      scope:location.origin+'|'+config.account+'|'+config.depot,warehouse:'公司大库'});
    const saved=recovery();
    if(saved.enabled!==setting.enabled) GM_setValue(recoveryKey,{...saved,enabled:setting.enabled===true});
    nativeStatus=setting.status;
  }
  async function reportRecovery(state) {
    const config=GM_getValue('inventory_connection',null);
    if(!config || Date.now()-lastRecoveryReport<10000) return;
    lastRecoveryReport=Date.now();
    await local('/recovery',{client_id:clientId,scope:location.origin+'|'+config.account+'|'+config.depot,warehouse:'公司大库',state}).catch(()=>{});
  }
  function recovery() { return GM_getValue(recoveryKey,{enabled:false,attempts:0,lastAttempt:0,pending:false}); }
  GM_registerMenuCommand('自动重登设置（在本机程序中开关）',()=>{
    alert('请在本机库存监控程序中勾选或取消“自动重登（无人值守）”。设置会保存，普通库存监控不受影响。');
  });
  async function loginTick() {
    if (loginBusy || location.pathname!=='/login.jsp') return;
    loginBusy=true;
    try {
      const attempt=async()=>{
        const config=GM_getValue('inventory_connection',null);
        if(config) await syncRecoverySetting(config);
        const state=recovery();
        if (state.enabled && GM_getValue('inventory_connection',null)) {
          const refreshed=refreshRecoveryState(state);
          if(refreshed) {
            // Save before navigation, so the new document cannot refresh in a loop.
            GM_setValue(recoveryKey,refreshed);
            badge.textContent='库存采集：重登前刷新页面';
            location.reload();
            return;
          }
        }
        const verification=document.querySelector('#verifydiv');
        const code=document.querySelector('#verifycode');
        const captcha=Boolean((verification && verification.getClientRects().length) || (code && code.getClientRects().length));
        const message=document.querySelector('#msg');
        const rejected=Boolean(message && message.getClientRects().length && /密码.*(?:错误|不正确)|(?:账号|账户|用户).*(?:错误|不存在|锁定|禁用)|登录.*频繁/.test(message.innerText));
        const action=loginDecision({enabled:state.enabled && Boolean(GM_getValue('inventory_connection',null)),captcha,
          rejected,requesting:Boolean(pageWindow.jQuery && pageWindow.jQuery.active>0),
          autofilled:browserFilled(document.querySelector('#username')) && browserFilled(document.querySelector('#password')),
          attempts:0,lastAttempt:0,now:Date.now(),readyAt:loginReadyAt});
        const hints={disabled:'自动重登未启用或未配置库存连接',captcha:'出现验证码，请人工登录',rejected:'登录被拒绝，请检查账号状态或密码',exhausted:'已点击 3 次，请人工检查登录',autofill:'等待浏览器自动填充账号密码',cooldown:'等待登录结果（点击至少间隔 5 秒）',settling:'刷新后等待浏览器填充（还需 '+Math.max(1,Math.ceil((loginReadyAt-Date.now())/1000))+' 秒）'};
        if(action!=='submit') {
          badge.textContent='库存采集：'+hints[action];
          if(['captcha','exhausted','autofill','rejected'].includes(action)) await reportRecovery(action==='rejected'?'exhausted':action);
          return;
        }
        const button=document.querySelector('#loginbtn');
        if(!button || button.disabled || !button.getClientRects().length) return;
        badge.textContent='库存采集：'+({exhausted:'本机重登已尝试 3 次，请人工检查',wrong_tab:'请切到 ERP 完成本机绑定',browser_unavailable:'本机浏览器连接不可用',manual_required:'需要人工验证'}[nativeStatus] || '等待本机程序点击登录');
        await reportRecovery('native_ready');
      };
      // Origin-scoped lock prevents multiple tabs submitting the same saved login.
      if (!navigator.locks) { badge.textContent='库存采集：浏览器不支持自动重登锁，请人工登录'; return; }
      await navigator.locks.request('inventory-erp-auto-login',{ifAvailable:true},async lock=>{if(lock) await attempt();});
    } catch (_) { badge.textContent='库存采集：请打开新版本机程序并检查自动重登开关'; }
    finally { loginBusy=false; }
  }
  if (location.pathname==='/login.jsp') {
    const loginTimer=setInterval(loginTick,1000);
    window.__inventoryMonitorInstance={stop(){clearInterval(loginTimer);badge.remove();delete window.__inventoryMonitorInstance;}};
    loginTick();
    return;
  }

  function beginRecovery() {
    const state=recovery();
    if (!state.enabled) return;
    const refreshed=refreshRecoveryState(state);
    if(refreshed) {
      GM_setValue(recoveryKey,refreshed);
      badge.textContent='库存采集：重登前刷新页面';
      location.reload();
      return;
    }
    GM_setValue(recoveryKey,{...state,pending:true});
    location.assign('/login.jsp');
  }

  function restoreInventoryPage(config) {
    const state=recovery();
    if (!state.enabled || !accountVisible(config.account)) return false;
    // Restore after a browser refresh too: pending may have been cleared by
    // the previous successful snapshot. Never derive the account from login inputs.
    const stockFrames=frames().filter(win=>win.location.pathname==='/pages/stock/stock_list.jsp');
    // Inventory may be an inactive ERP tab. Never reopen/focus its menu after
    // seeing it in this document, including when the user later closes that tab.
    if(stockFrames.length) stockPageSeen=true;
    else if(stockPageSeen) return false;
    // Only navigate visible inventory menus and select a known warehouse filter.
    for (const win of stockFrames) {
      for (const select of win.document.querySelectorAll('select')) {
        if(!select.getClientRects().length) continue;
        const option=[...select.options].find(o=>o.textContent.trim()==='公司大库' && o.value===config.depot);
        if(option && select.value!==option.value) { searchedStockPages.delete(win.document); select.value=option.value; select.dispatchEvent(new win.Event('change',{bubbles:true})); return true; }
      }
      if(win.mini) for(const id of ['depot_id','depotIds','depotId']) {
        const control=win.mini.get(id);
        if(!control || !control.el || !control.el.getClientRects().length || typeof control.getData!=='function') continue;
        const options=control.getData();
        const valueField=control.valueField||'id', textField=control.textField||'text';
        const option=Array.isArray(options) && options.find(o=>String(o[valueField])===config.depot && o[textField]==='公司大库');
        if(option && String(control.getValue())!==config.depot) { searchedStockPages.delete(win.document); control.setValue(config.depot); control.fire('valuechanged'); return true; }
      }
      try {
        if(warehouseProof()===config.depot && !searchedStockPages.has(win.document)) {
          const control=win.mini && win.mini.get('depot_id');
          const toolbar=control && control.el && control.el.closest('.mini-toolbar');
          const buttons=toolbar ? [...toolbar.querySelectorAll('a.mini-button,button')].filter(el=>el.getClientRects().length && el.textContent.trim()==='搜索') : [];
          if(buttons.length!==1) return true;
          buttons[0].click();
          searchedStockPages.add(win.document);
          return true;
        }
      } catch (_) {}
    }
    try { if(warehouseProof()===config.depot) return false; } catch (_) {}
    // MiniUI options load asynchronously. Keep waiting for the existing frame.
    if(stockFrames.length) return true;
    // Live ERP uses div dropdown items, not links/spans. Invoke its existing
    // inventory-menu handler; it opens the stock tab without touching ERP data.
    const stockMenus=[...document.querySelectorAll('.menu-item .dropdown-item[title="分库库存"]')]
      .filter(el=>el.textContent.trim()==='分库库存');
    if(stockMenus.length===1 && !clickedMenus.has('分库库存')) {
      clickedMenus.add('分库库存');
      stockMenus[0].click();
      return true;
    }
    for(const label of ['分库库存','库房管理']) {
      if(clickedMenus.has(label)) continue;
      const elements=[...document.querySelectorAll('a,span')].filter(el=>el.getClientRects().length && el.textContent.trim()===label);
      const leaves=elements.filter(el=>!elements.some(other=>other!==el && el.contains(other)));
      if(leaves.length===1) { clickedMenus.add(label); leaves[0].click(); return true; }
    }
    return true;
  }

  function frames(win=pageWindow, depth=0) {
    const out=[win];
    if (depth<6) for (const frame of win.document.querySelectorAll('iframe')) {
      try { if (frame.contentWindow.document) out.push(...frames(frame.contentWindow,depth+1)); } catch (_) {}
    }
    return out;
  }

  function warehouseProof() {
    const matches=[];
    for (const win of frames()) {
      for (const select of win.document.querySelectorAll('select')) {
        const option=select.selectedOptions && select.selectedOptions[0];
        if (option && option.textContent.trim()==='公司大库' && /^\d+$/.test(option.value))
          matches.push(option.value);
      }
      if (win.mini) for (const id of ['depot_id','depotIds','depotId']) {
        try {
          const control=win.mini.get(id);
          if (control && control.getText().trim()==='公司大库' && /^\d+$/.test(String(control.getValue())))
            matches.push(String(control.getValue()));
        } catch (_) {}
      }
    }
    const unique=[...new Set(matches)];
    if (unique.length!==1) throw new Error('请保持分库库存页打开，并选择公司大库');
    return unique[0];
  }

  function accountVisible(name) {
    // The chosen account must remain visible in the ERP header. Never inspect
    // cookies, storage, hidden inputs, or authentication globals.
    const header=document.querySelector('.topNav') || document.querySelector('.navbar');
    if (!header || !name) return false;
    return [...header.querySelectorAll('a,span')].some(el=>el.getClientRects().length && el.innerText.trim()===name);
  }

  function configure() {
    try {
      const depot=warehouseProof();
      const account=prompt('请输入 ERP 顶栏当前显示的账号姓名（仅用于区分库存，不是密码）');
      if (!account) return;
      if (!accountVisible(account.trim())) throw new Error('顶栏未找到这个账号名称，配置未保存');
      GM_setValue('inventory_connection', {depot,account:account.trim()});
      nextPoll=0;
      badge.textContent='库存采集：等待连接';
    } catch(error) { alert(error.message); }
  }
  badge.addEventListener('click',configure);
  GM_registerMenuCommand('连接公司大库库存监控',configure);

  function local(path, body) {
    return new Promise((resolve,reject)=>GM_xmlhttpRequest({method:'POST',
      url:'http://127.0.0.1:18763'+path,
      headers:{'Content-Type':'application/json','X-Inventory-Client':'erp-userscript-v1'},
      data:JSON.stringify(body),timeout:20000,
      onload:r=>{ try { const data=JSON.parse(r.responseText); if(r.status!==200) throw new Error(data.error || '本机连接失败'); resolve(data); } catch(e) { reject(e); } },
      onerror:()=>reject(new Error('请打开钉钉货源监控桌面程序')),
      ontimeout:()=>reject(new Error('本机库存连接超时'))
    }));
  }

  async function warehouseDetails(goodsId) {
    if (!/^[1-9]\d{0,19}$/.test(String(goodsId))) throw new Error('分库查询缺少真实商品编号');
    const response=await fetch('/pages/stock/searchStockByGoodsId.htm', {
      method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','X-Requested-With':'XMLHttpRequest'},
      body:new URLSearchParams({goodsId:String(goodsId),pageIndex:'0',pageSize:'100'}),signal:AbortSignal.timeout(30000)
    });
    if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('json'))
      throw new Error('分库查询失败，请检查 ERP 登录状态');
    return normalizeWarehouseResponse(await response.json(),goodsId);
  }

  async function tick() {
    if (busy || Date.now()<nextPoll) return;
    busy=true;
    let identity;
    try {
      const config=GM_getValue('inventory_connection',null);
      if (!config) { badge.textContent='库存采集：点击连接'; return; }
      await syncRecoverySetting(config);
      if(frames().some(win=>win.location.pathname==='/login.jsp')) { beginRecovery(); return; }
      if(restoreInventoryPage(config)) {
        badge.textContent='库存采集：正在恢复公司大库页面';
        nextPoll=Date.now()+2000;
        return;
      }
      const depot=warehouseProof();
      if (depot!==config.depot || !accountVisible(config.account)) throw new Error('账号或仓库不匹配，采集已暂停');
      identity={client_id:clientId, scope:location.origin+'|'+config.account+'|'+depot,warehouse:'公司大库'};
      const job=await local('/poll',{...identity,capabilities:['warehouse_details_v1']});
      if(job.collector_error) throw new Error(job.collector_error);
      if (!job.check_id) { badge.textContent='库存采集：已连接'; return; }
      badge.textContent='库存采集：正在读取';
      if (job.job_type==='warehouse_lookup') {
        let details;
        try { details=await warehouseDetails(job.goods_id); }
        catch (_) { details={goods_id:String(job.goods_id),complete:false,error:'分库查询失败'}; }
        if (warehouseProof()!==depot || !accountVisible(config.account)) throw new Error('查询期间账号或仓库变化');
        await local('/warehouse-result',{...identity,check_id:job.check_id,request_id:job.request_id,details});
        badge.textContent=details.complete?'库存采集：分库列表已更新':'库存采集：分库查询失败，请重试';
        return;
      }
      const raw=await collectPages(async page=>{
        await local('/renew',{...identity,check_id:job.check_id});
        const body=new URLSearchParams({filter:'',depotIds:depot,b_stock:'0',search_category:'',
          search_out_stock:'0',search_zero_stock:'0',pageSize:'2000',pageIndex:String(page),sortField:'',sortOrder:''});
        const response=await fetch('/pages/stock/searchDeoptStockList.htm',{
          method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','X-Requested-With':'XMLHttpRequest'},
          body,signal:AbortSignal.timeout(30000)});
        if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('json')) {
          const error=new Error('ERP 登录失效或库存请求失败');
          error.loginExpired=response.redirected && new URL(response.url).origin===location.origin && new URL(response.url).pathname==='/login.jsp';
          throw error;
        }
        return response.json();
      });
      const rows=raw.map(normalizeCatalogRow);
      const warehouse_details={}, exceptions=new Set();
      for (const p of job.warehouse_products || []) {
        exceptions.add(p.sku);
        try {
          await local('/renew',{...identity,check_id:job.check_id});
          const product=rows.find(r=>r.sku===p.sku);
          if (!product || product.goods_id!==String(p.goods_id)) throw new Error('商品编号不匹配');
          warehouse_details[p.sku]=await warehouseDetails(p.goods_id);
        } catch (_) {
          warehouse_details[p.sku]={goods_id:String(p.goods_id),complete:false,error:'分库查询失败'};
        }
      }
      const journals={};
      const stamp=seconds=>new Date(seconds*1000+8*3600000).toISOString().slice(0,10)+' 00:00';
      for(const sku of job.skus) {
        if(exceptions.has(sku)) continue;
        const product=rows.find(r=>r.sku===sku);
        if(!product || product.stock_unknown) continue;
        if(!product.goods_id) throw new Error('重点产品缺少真实商品ID');
        const ledger=await collectPages(async page=>{
          await local('/renew',{...identity,check_id:job.check_id});
          const body=new URLSearchParams({goods_id:product.goods_id,depotName:depot,
            startTime:stamp(Math.min((job.journal_since[sku] || Date.now()/1000)-86400,Date.now()/1000-30*86400)),
            endTime:stamp(Date.now()/1000+86400),pageSize:'2000',pageIndex:String(page)});
          const response=await fetch('/pages/stock/searchInvAccDepotStock.htm',{method:'POST',credentials:'same-origin',
            headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest'},body,signal:AbortSignal.timeout(30000)});
          if(!response.ok || response.redirected) throw new Error('入库流水查询失败');
          return response.json();
        });
        const repeats=new Map();
        journals[sku]=ledger.map(row=>{const base=normalizeJournal(row,sku);const n=repeats.get(base.id)||0;repeats.set(base.id,n+1);return normalizeJournal(row,sku,n);});
      }
      // Recheck identity after all pages before accepting any snapshot.
      if (warehouseProof()!==depot || !accountVisible(config.account)) throw new Error('采集期间账号或仓库变化');
      const accepted=await local('/snapshot',{...identity,check_id:job.check_id,complete:true,rows,journals,warehouse_details});
      if (accepted.retry) { badge.textContent='库存采集：设置已更新，等待重新采集'; return; }
      const unknown=rows.filter(r=>r.stock_unknown).length;
      badge.textContent='库存采集：已更新'+(unknown?'，'+unknown+' 件商品库存未知':'');
      const recovered=recovery();
      if(recovered.pending || recovered.attempts) GM_setValue(recoveryKey,{...recovered,pending:false,refreshed:false,attempts:0,lastAttempt:0});
    } catch(error) {
      badge.textContent='库存采集：'+error.message;
      if(identity) await local('/error',{client_id:clientId}).catch(()=>{});
      if(error.loginExpired) beginRecovery();
      nextPoll=Date.now()+60000;
    } finally {
      if(nextPoll<=Date.now()) nextPoll=Date.now()+20000;
      busy=false;
    }
  }
  const timer=setInterval(tick,2000);
  window.__inventoryMonitorInstance={stop(){clearInterval(timer);badge.remove();delete window.__inventoryMonitorInstance;}};
  tick();
})();
