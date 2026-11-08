// 从候选的实际 HTTP 服务验收只读契约，不模拟数据接口、不打印连接令牌。
const assert=require('node:assert/strict');
const fs=require('node:fs');
async function main(){
 const address=new URL(process.argv[2]);const token=address.hash.slice(1);const origin=address.origin;
 if(address.hostname!=='127.0.0.1')throw Error('仅允许隔离本机服务');
 async function call(path,body,credential=token){
  const response=await fetch(origin+path,{method:body?'POST':'GET',headers:{'X-Console-Token':credential,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return {status:response.status,data:await response.json()};
 }
 const connection=(await call('/api/data/connection')).data;
 assert.equal(connection.scope,'read-only');
 const catalog=await call('/api/data/catalog',null,connection.token);
 assert.equal(catalog.status,200);assert.equal(catalog.data.datasets.length,27);
 for(const dataset of catalog.data.datasets){
  const response=await call('/api/data/query',{dataset:dataset.id},connection.token);
  assert.equal(response.status,200,dataset.id+': '+JSON.stringify(response.data));
  assert.equal(response.data.read_only,true);assert.equal(response.data.live_query,false);
  for(const row of response.data.rows)assert.deepEqual(Object.keys(row).sort(),dataset.fields.toSorted());
 }
 const first=(await call('/api/data/query',{dataset:'orders',page_size:20},connection.token)).data;
 assert.equal(first.total,83);const ids=[];
 for(let page=0;page<5;page++){
  const result=await call('/api/data/query',{dataset:'orders',page_size:20,page,snapshot:first.snapshot},connection.token);
  assert.equal(result.status,200);ids.push(...result.data.rows.map(row=>row.id));
 }
 assert.equal(ids.length,83);assert.equal(new Set(ids).size,83);
 const scoped=(await call('/api/data/query',{dataset:'orders',filters:[{field:'shop',op:'eq',value:'76'}]},connection.token)).data;
 assert.ok(scoped.rows.length>0);assert.ok(scoped.rows.every(row=>row.shop==='76'));
 for(const [path,body] of [['/api/data/connection',null],['/api/orders/action',{action:'start'}],['/api/business/action',{}],['/api/update/install',{}]]){
  assert.equal((await call(path,body,connection.token)).status,403,path);
 }
 assert.equal((await call('/api/data/query',{dataset:'inventory',sql:'SELECT *'},connection.token)).data.code,'INVALID_QUERY');
 const refreshed=await call('/api/data/refresh',{dataset:'price_changes',request_id:'api-price-candidate-001'},connection.token);
 assert.equal(refreshed.status,200);let job;
 for(let attempt=0;attempt<100;attempt++){
  job=await call('/api/data/job?id='+encodeURIComponent(refreshed.data.job),null,connection.token);
  if(job.data.state!=='running')break;
  await new Promise(resolve=>setTimeout(resolve,100));
 }
 assert.equal(job.data.state,'done',JSON.stringify(job.data));
 const prices=(await call('/api/data/query',{dataset:'price_changes'},connection.token)).data;
 assert.equal(prices.rows[0].delta_cents,100);
 assert.equal((await call('/api/data/query',{dataset:'price_affected_configs'},connection.token)).data.rows[0].cost_delta_cents,200);
 assert.equal((await call('/api/data/query',{dataset:'price_events'},connection.token)).data.total,0);
 const {spawnSync}=require('node:child_process');
 const cli=spawnSync('D:/python/python.exe',['-B','-X','utf8','tools/query_inventory_data.py','--url',origin],{env:{...process.env,INVENTORY_QUERY_TOKEN:connection.token},encoding:'utf8'});
 assert.equal(cli.status,0);assert.equal(JSON.parse(cli.stdout).datasets.length,27);
 fs.mkdirSync('outputs/机器人查询验收',{recursive:true});
 fs.writeFileSync('outputs/机器人查询验收/result.json',JSON.stringify({passed:true,datasets:27,orderRows:83,uniquePages:true,shopFilter:true,writeDenied:true,priceRefresh:true,noNotifications:true,cli:true},null,2));
 console.log('机器人查询验收通过：27 数据集、83条完整分页、店铺筛选、只读权限隔离。');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
