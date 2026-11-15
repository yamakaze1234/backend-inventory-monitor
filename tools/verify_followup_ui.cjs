// 用隔离接口驱动真实前端，验证历史回执不会在导航或刷新后重新打开权限窗口。
const fs=require('node:fs');const path=require('node:path');const http=require('node:http');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
async function main(){
 const root=path.resolve('app/ui/console');
 const server=http.createServer((req,res)=>{
  const file=path.resolve(root,'.'+new URL(req.url,'http://localhost').pathname.replace(/\/$/,'/index.html'));
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){res.writeHead(404).end();return;}
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const key='business-job-environment-settings';let serial=0;let running=false;let reads=0;
  const nextForm={action:'ops-members-save',title:'编辑成员权限',module:'settings',fields:[],values:{}};
  await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url());let data={};
   if(url.pathname==='/api/info')data={version:'test',autoCheck:false};
   else if(url.pathname==='/api/inventory')data={products:[],connection:'offline'};
   else if(url.pathname.startsWith('/api/business/view/'))data={module:url.pathname.split('/').pop(),filters:[],query:{},sections:[{id:'robot-binding',title:'机器人、群聊与权限',forms:[{action:'ops-members-list',title:'编辑成员权限',module:'settings',immediate:true,fields:[],values:{}}],tables:[]}]};
   else if(url.pathname==='/api/business/action')data={job:'new-'+(++serial)};
   else if(url.pathname==='/api/business/job'){reads++;data={id:url.searchParams.get('id'),state:running?'running':'done',result:{message:'已读取权限',nextForm}};}
   await route.fulfill({json:data});
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  const enter=()=>page.getByRole('button',{name:'环境配置',exact:true}).click();
  const leave=()=>page.getByRole('button',{name:'配件库存监控',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'编辑成员权限',exact:true});
  const idle=async()=>{await page.getByText('已读取权限',{exact:true}).waitFor();await page.waitForTimeout(150);};
  // 旧版本遗留的已完成回执：进入页面仅显示结果，不能自动弹窗。
  await page.evaluate(k=>sessionStorage.setItem(k,'old-job'),key);await enter();await idle();
  assert.equal(await dialog.count(),0,'历史回执不应自动打开权限窗口');
  assert.equal(await page.evaluate(k=>sessionStorage.getItem(k),key),null);
  await page.getByRole('button',{name:'继续：编辑成员权限',exact:true}).click();await dialog.waitFor();
  await dialog.getByRole('button',{name:'取消',exact:true}).click();
  // 本次主动点击仍自动接续，关闭后轮询、切页和刷新均不能重开。
  await page.getByRole('button',{name:'编辑成员权限',exact:true}).click();await dialog.waitFor();
  await dialog.getByRole('button',{name:'取消',exact:true}).click();
  const oldReads=reads;await page.waitForFunction(()=>document.querySelector('[data-business-module="environment-settings"]'));
  await page.waitForTimeout(1200);assert.ok(reads>oldReads);assert.equal(await dialog.count(),0);
  await leave();await enter();await page.waitForTimeout(300);assert.equal(await dialog.count(),0);
  await page.reload();await enter();await page.waitForTimeout(300);assert.equal(await dialog.count(),0);
  // 操作未完成就离开：回来后保留回执和手动继续入口，不抢焦点。
  running=true;await page.getByRole('button',{name:'编辑成员权限',exact:true}).click();
  await page.waitForFunction(k=>!!sessionStorage.getItem(k),key);await leave();await enter();
  running=false;await idle();assert.equal(await dialog.count(),0);
  await page.getByRole('button',{name:'继续：编辑成员权限',exact:true}).click();await dialog.waitFor();
  assert.equal(serial,2,'打开历史表单不应重新提交业务操作');assert.deepEqual(errors,[]);
  console.log('PASS: 历史回执、主动打开、取消、轮询、切页、刷新、在途恢复及手动继续');
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
