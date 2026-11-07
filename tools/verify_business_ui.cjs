// 完整 ReUI 页验收：只连接隔离样例服务，不模拟业务页面、不执行外部发送。
const fs=require('node:fs');const path=require('node:path');const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
async function main(){
 const url=process.argv[2];if(!url?.startsWith('http://127.0.0.1:'))throw Error('需要隔离地址');
 const output=path.resolve('outputs/ReUI业务验收');fs.mkdirSync(output,{recursive:true});
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);
  const nav=[['douyin','抖店库存',['整机库存','补库存提醒','库存变化','店铺管理','本店连接与维护表','低库存预警','人工排除链接']],['dingtalk','钉钉监控',['消息监听']],['profit','ERP 价格监控',['价格监控配置','检测结果']],['daily','货源汇报',['货源周报','定时日报','AI 接入','接收机器人']],['events','库存提醒记录',['库存提醒记录']],['monitor','管理监控商品',['关注产品','搜索 ERP 商品','本轮检查明细']],['settings','连接与通知设置',['库存数据来源','通知机器人','本地缓存']]];
  async function section(title){await page.getByRole('combobox',{name:'业务分区'}).click();await page.getByRole('option',{name:title,exact:true}).click();}
  for(const [module,title,sections] of nav){
   await page.getByRole('button',{name:title,exact:true}).click();await page.locator(`[data-business-module="${module}"] [data-slot="frame"]`).first().waitFor();
   for(const title of sections){await section(title);assert.equal(await page.locator('[data-native-module],iframe').count(),0);await page.screenshot({path:path.join(output,`${module}-${sections.indexOf(title)}.png`),fullPage:true,animations:'disabled'});}
   assert.equal(await page.getByRole('alert').count(),0,`${module} 读取失败`);
  }
  await page.getByRole('button',{name:'管理监控商品',exact:true}).click();
  await page.getByRole('button',{name:'添加产品',exact:true}).click();
  let dialog=page.getByRole('dialog');await dialog.getByLabel('SKU 编码',{exact:true}).fill('REUI-TEST');await dialog.getByLabel('ERP 商品编号',{exact:true}).fill('12345');await dialog.getByLabel('商品名称',{exact:true}).fill('ReUI 隔离测试商品');await dialog.getByLabel('预警阈值',{exact:true}).fill('7');await dialog.getByRole('button',{name:'提交',exact:true}).click();
  await page.getByText('产品配置已保存',{exact:true}).waitFor();
  await page.getByRole('textbox',{name:'筛选已关注产品',exact:true}).fill('REUI-TEST');
  await page.getByRole('button',{name:'编辑产品',exact:true}).click();dialog=page.getByRole('dialog');
  assert.equal(await dialog.getByLabel('预警阈值',{exact:true}).inputValue(),'7');assert.equal(await dialog.getByLabel('SKU 编码',{exact:true}).getAttribute('readonly'),'');await dialog.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('button',{name:'批量移除关注',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByRole('checkbox',{name:'ReUI 隔离测试商品 · REUI-TEST',exact:true}).check();await dialog.getByRole('button',{name:'提交',exact:true}).click();
  await page.getByRole('button',{name:'确认移除关注',exact:true}).waitFor();await page.getByRole('button',{name:'确认移除关注',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'提交',exact:true}).click();await page.getByRole('alertdialog').getByRole('button',{name:'确认执行',exact:true}).click();await page.getByText('已移除 1 个关注产品',{exact:true}).waitFor();
  await page.getByRole('button',{name:'连接与通知设置',exact:true}).click();await section('库存数据来源');await page.getByRole('button',{name:'保存监控周期',exact:true}).click();await page.getByRole('dialog').getByLabel('分钟',{exact:true}).fill('37');await page.getByRole('dialog').getByRole('button',{name:'提交',exact:true}).click();await page.getByText('监控周期已保存',{exact:true}).waitFor();
  await section('通知机器人');await page.getByRole('button',{name:'添加机器人',exact:true}).click();dialog=page.getByRole('dialog');assert.equal(await dialog.getByLabel('Webhook（留空保留原地址）',{exact:true}).getAttribute('type'),'password');assert.equal(await dialog.getByLabel('钉钉账号标识',{exact:true}).count(),0);await dialog.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('button',{name:'切换深色',exact:true}).click();await page.screenshot({path:path.join(output,'settings-dark.png'),fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:390,height:844});
  for(const [module,title,sections] of nav){
   await page.getByRole('button',{name:'展开或收起导航'}).click();await page.getByRole('button',{name:title,exact:true}).click();await page.locator(`[data-business-module="${module}"]`).waitFor();
   for(const title of sections){await section(title);const width=await page.evaluate(()=>[document.documentElement.clientWidth,document.documentElement.scrollWidth]);assert.ok(width[1]<=width[0],`${module}/${title} 页面横向溢出 ${width}`);}
   await page.screenshot({path:path.join(output,module+'-mobile.png'),fullPage:true,animations:'disabled'});
  }
  assert.deepEqual(errors,[]);assert.equal(page.context().pages().length,1);
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({passed:true,modules:nav.map(x=>x[0]),sections:nav.reduce((n,x)=>n+x[2].length,0),productCreateEditRemove:true,settingsSave:true,passwordMasked:true,mobileWidth:390,errors},null,2));
  console.log('ReUI 业务验收通过：七个真实页面、21 个分区、表单写入、预览确认、深色与窄屏。');
 }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
