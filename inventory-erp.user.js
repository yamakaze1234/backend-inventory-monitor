// ==UserScript==
// @name         公司大库重点产品采集
// @namespace    local.inventory.monitor
// @version      0.2.0
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

  if (typeof module !== 'undefined' && module.exports) {
    module.exports={collectPages, normalizeRow, normalizeCatalogRow, normalizeJournal, normalizeWarehouseResponse};
    return;
  }
  if (window.top!==window.self || location.pathname.includes('login')) return;
  if (window.__inventoryMonitorInstance) return;
  const pageWindow=typeof unsafeWindow==='undefined'?window:unsafeWindow;
  const clientId=crypto.randomUUID();
  let busy=false, nextPoll=0;
  const badge=document.createElement('button');
  badge.textContent='库存采集：未连接';
  badge.style.cssText='position:fixed;bottom:10px;right:18px;z-index:99999;background:#101C30;color:white;border:0;padding:9px 14px;font:13px Microsoft YaHei;cursor:pointer';
  badge.title='点击配置；数据只发送到本机库存监控程序';
  document.body.appendChild(badge);

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
        if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('json'))
          throw new Error('ERP 登录失效或库存请求失败');
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
    } catch(error) {
      badge.textContent='库存采集：'+error.message;
      if(identity) await local('/error',{client_id:clientId}).catch(()=>{});
      nextPoll=Date.now()+60000;
    } finally { busy=false; }
  }
  const timer=setInterval(tick,20000);
  window.__inventoryMonitorInstance={stop(){clearInterval(timer);badge.remove();delete window.__inventoryMonitorInstance;}};
  tick();
})();
