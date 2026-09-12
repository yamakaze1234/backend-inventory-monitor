const test = require('node:test');
const assert = require('node:assert/strict');
const {collectPages, normalizeRow, normalizeJournal} = require('./inventory-erp.user.js');
test('null available stock remains explicitly unknown in catalog collection',()=>{
  const {normalizeCatalogRow}=require('./inventory-erp.user.js');
  const row={b_c_sku:'A',goods_id:436535,n_stock_able:null,n_stock:0,n_stock_purchase:null};
  assert.deepEqual(normalizeCatalogRow(row),{sku:'A',goods_id:'436535',name:'',able:null,stock:0,purchase:0,stock_unknown:true});
  assert.throws(()=>normalizeCatalogRow({...row,n_stock_able:'bad'}));
});

test('native total-stock details select real depot identities, never the stock-record id',()=>{
  const {normalizeWarehouseResponse}=require('./inventory-erp.user.js');
  assert.equal(typeof normalizeWarehouseResponse,'function');
  const raw={result:true,total:0,code:0,data:[
    {goods_id:88102,depot_id:131,id:2617228,depot_name:'稀缺货源',n_stock:20},
    {goods_id:88102,depot_id:125,id:2617208,depot_name:'样品库',n_stock:1}]};
  assert.deepEqual(normalizeWarehouseResponse(raw,'88102'),{goods_id:'88102',complete:true,depots:[
    {id:'131',name:'稀缺货源',stock:20,able:null,purchase:null},
    {id:'125',name:'样品库',stock:1,able:null,purchase:null}]});
  assert.throws(()=>normalizeWarehouseResponse(raw,'999'));
  assert.throws(()=>normalizeWarehouseResponse({...raw,data:[raw.data[0],raw.data[0]]},'88102'));
  assert.throws(()=>normalizeWarehouseResponse({...raw,result:false},'88102'));
  assert.throws(()=>normalizeWarehouseResponse({...raw,code:1},'88102'));
});

test('warehouse missing stock stays unknown while explicit zero and negatives are retained',()=>{
  const {normalizeWarehouseResponse}=require('./inventory-erp.user.js');
  assert.equal(typeof normalizeWarehouseResponse,'function');
  const row={goods_id:88102,depot_id:131,depot_name:'稀缺货源'};
  const make=n=>normalizeWarehouseResponse({result:true,data:[{...row,...n}]},'88102').depots[0];
  assert.equal(make({}).stock,null);
  assert.equal(make({n_stock:0}).stock,0);
  assert.equal(make({n_stock:-1}).stock,-1);
  assert.throws(()=>make({n_stock:''}));
});

test('quantity errors identify field and goodsid without echoing raw values',()=>{
  const row={b_c_sku:'A',goods_id:57339,n_stock_able:1,n_stock_purchase:0,n_stock:2};
  assert.throws(()=>normalizeRow({...row,n_stock_able:null}), /商品编号57339.*可用库存.*null/);
  assert.throws(()=>normalizeRow({...row,n_stock:undefined}), /实际库存.*缺失/);
  assert.throws(()=>normalizeRow({...row,n_stock_purchase:'private-test-value'}), e=>
    /待入数量.*非数字/.test(e.message) && !e.message.includes('private-test-value'));
  assert.throws(()=>normalizeRow({...row,n_stock:'   '}), /实际库存.*空白/);
  assert.throws(()=>normalizeJournal({n_in:undefined},'A'), /入库流水.*入库数量.*缺失/);
});

test('collects all pages and rejects early empty pages', async () => {
  let calls=[];
  const rows=await collectPages(async i=>{calls.push(i); return {total:3,data:i===0?[{id:1},{id:2}]:[{id:3}]};});
  assert.equal(rows.length,3); assert.deepEqual(calls,[0,1]);
  await assert.rejects(()=>collectPages(async i=>({total:3,data:i===0?[{id:1}]:[]})));
});
test('rejects malformed business responses and changing totals', async()=>{
  await assert.rejects(()=>collectPages(async()=>({success:false,message:'登录失效'})));
  await assert.rejects(()=>collectPages(async i=>({total:i?2:3,data:[{id:i}]})));
});
test('preserves negatives and fails missing numerical fields',()=>{
  const row={b_c_sku:'A',b_c_name:'产品',n_stock_able:-2,n_stock_purchase:0,n_stock:1};
  assert.equal(normalizeRow(row).able,-2);
  assert.throws(()=>normalizeRow({...row,n_stock_able:null}));
  assert.throws(()=>normalizeRow({...row,n_stock_able:'bad'}));
});
test('ERP null pending means no pending count, not invalid actual stock',()=>{
  const row={b_c_sku:'A',goods_id:1,b_c_name:'产品',n_stock_able:0,n_stock_purchase:null,n_stock:0};
  assert.equal(normalizeRow(row).purchase,0);
  assert.equal(normalizeRow(row).goods_id,'1');
});
test('business rejection with total cannot be accepted',async()=>{
  await assert.rejects(()=>collectPages(async()=>({result:false,total:0,data:[]})));
});
test('inbound uses bill kind and has stable fingerprint',()=>{
  const r={c_name:'公司大库',create_time:'2026-09-05 15:04:41',c_billcode:'RK-TEST',c_type:'入库单',n_begin:14,n_in:5,n_out:null,n_end:19};
  assert.equal(normalizeJournal(r,'A').in_qty,5);
  assert.equal(normalizeJournal(r,'A').id,normalizeJournal(r,'A').id);
  assert.equal(normalizeJournal(r,'A').id,normalizeJournal({...r,n_begin:19,n_end:24},'A').id);
});
