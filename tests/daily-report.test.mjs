import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {collectReport,renderReport,dateRange} from '../integrations/order_backend/control/report.mjs';
import {loadConfig,directEnvironment,safeError} from '../integrations/order_backend/control/daily-report-config.mjs';
import {RpcClient,connectionMetadata} from '../integrations/order_backend/control/mcp-client.mjs';

const root=await fs.mkdtemp(path.join(os.tmpdir(),'daily-report-test-'));
const config={workspace:root,python:path.resolve('.venv/Scripts/python.exe'),connectionName:'fixture',connectionId:'id',database:'ERP'};
const row={出库时间:'2020-01-01T12:00:00',单据类型:'销售',项目组:'抖英特旗舰组',整机数量:1,销售金额:100,处理器:'测试',显卡系列:'测试'};
function factory(count=101,bad=false){
  let calls=0;
  return async()=>({reportConfig:config,close(){},async request(method,args){
    assert.equal(args.arguments.connection_name,'fixture');assert.equal(args.arguments.connection_id,'id');
    const sql=args.arguments.sql;assert.ok(sql.includes("[项目组]=N'抖英特旗舰组'"));
    let rows;
    if(sql.includes('COUNT_BIG'))rows=[{row_count:count,quantity:count,amount:count*100,missing_amount:0,missing_quantity:0}];
    else{calls++;rows=Array.from({length:bad?0:Math.min(100,count-(calls-1)*100)},()=>row);}
    return {content:[{type:'text',text:'| row_json |\n|---|\n'+rows.map(x=>'| '+JSON.stringify(x)+' |').join('\n')+(rows.length?'':'\n(0 rows)')}]};
  }});
}
test('多页数据与两种文件输出通过，测试目录隔离',async()=>{
  const {dir,payload}=await collectReport('2020-01-01',{test:true,clientFactory:factory()});
  assert.equal(payload.rows.length,101);assert.ok(dir.includes('daily-report-tests'));
  const result=await renderReport(dir,payload);assert.equal(result.complete,true);
  for(const f of result.files)assert.ok((await fs.stat(f)).size>0);
});
test('截断页拒绝成功',async()=>{await assert.rejects(collectReport('2020-01-01',{test:true,clientFactory:factory(101,true)}),/分页不完整/);});
test('空日期允许正常零数据',async()=>{const r=await collectReport('2020-01-01',{test:true,clientFactory:factory(0)});assert.equal(r.payload.rows.length,0);});
test('未来和当日日期拒绝',()=>{assert.throws(()=>dateRange('2999-01-01'));});
test('缺配置不回退',async()=>{process.env.ORDER_BOT_CONFIG=path.join(root,'order-bot.json');await assert.rejects(loadConfig(),/配置/);});
test('子进程启动失败及时返回',async()=>{const c=new RpcClient(path.join(root,'missing.exe'));try{await assert.rejects(c.request('initialize',{},1000));}finally{c.close();}});
test('错误脱敏且直连不继承代理',()=>{assert.ok(!safeError(new Error('login failed password=secret')).includes('secret'));process.env.HTTPS_PROXY='http://fixture';assert.equal(directEnvironment(config).HTTPS_PROXY,undefined);});
test('公开元数据选择不回显其他字段',async()=>{const rows=await connectionMetadata({request:async()=>({content:[{type:'text',text:'| ID | Name | Group Path | Type | Host | Port | Database |\n| --- | --- | --- | --- | --- | --- | --- |\n| x | fixture | | sqlserver | 127.0.0.1 | 1433 | ERP |'}]})});assert.equal(rows[0].id,'x');assert.equal(rows[0].host,'127.0.0.1');});

test('配置快照不被后续保存修改',async()=>{
  const file=path.join(root,'daily-report.json');
  const value={version:1,networkMode:'direct',connectionId:'a',connectionName:'A',database:'ERP',dbxExecutable:path.join(root,'dbx.exe'),dbxData:root,python:config.python};
  await fs.writeFile(file,JSON.stringify(value));const before=await loadConfig();
  await fs.writeFile(file,JSON.stringify({...value,connectionId:'b',connectionName:'B'}));const after=await loadConfig();
  assert.equal(before.connectionName,'A');assert.equal(after.connectionName,'B');assert.ok(Object.isFrozen(before));
});
test('文件生成失败不写成功回执',async()=>{
  const {dir,payload}=await collectReport('2020-01-01',{test:true,clientFactory:factory(0)});
  await fs.writeFile(path.join(dir,'runtime.json'),JSON.stringify({python:path.join(root,'missing.exe')}));
  await assert.rejects(renderReport(dir,payload),/文件生成失败/);
  await assert.rejects(fs.access(path.join(dir,'result.json')));
});
