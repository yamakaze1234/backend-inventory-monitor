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
  await page.getByRole('button',{name:'添加产品',exact:true}).click();
  await page.locator('[data-business-module="monitor"]').waitFor();
  const quickAdd=page.getByRole('dialog',{name:'添加产品',exact:true});
  await quickAdd.getByLabel('商品名称或 ERP ID',{exact:true}).waitFor();
  await quickAdd.getByRole('button',{name:'取消',exact:true}).click();
  const nav=[['douyin','前台库存监控',['整机库存','补库存提醒','库存变化','店铺管理','本店连接与维护表','低库存预警','人工排除链接']],['dingtalk','钉钉货源监控',['消息监听','货源周报','定时日报','AI 接入','接收机器人']],['profit','ERP调价监控',['查看价格变动','价格监控配置','检测结果']],['events','库存提醒记录',['库存提醒记录']],['monitor','管理监控商品',['关注产品','搜索 ERP 商品','本轮检查明细']],['order-settings','订单机器人设置',['匹配服务配置','订单匹配机器人']],['settings','连接与通知设置',['部门与工作区','库存数据来源','通知机器人','本地缓存']]];
  async function section(title){await page.getByRole('combobox',{name:'业务分区'}).click();await page.getByRole('option',{name:title,exact:true}).click();}
  for(const [module,title,sections] of nav){
   await page.getByRole('button',{name:title,exact:true}).click();await page.locator(`[data-business-module="${module}"] [data-slot="frame"]`).first().waitFor();
   for(const title of sections){await section(title);assert.equal(await page.locator('[data-native-module],iframe').count(),0);await page.screenshot({path:path.join(output,`${module}-${sections.indexOf(title)}.png`),fullPage:true,animations:'disabled'});}
   assert.equal(await page.getByRole('alert').count(),0,`${module} 读取失败`);
  }
  await page.getByRole('button',{name:'订单机器人设置',exact:true}).click();await section('匹配服务配置');
  await page.getByRole('button',{name:'一键配置',exact:true}).click();
  await page.getByRole('button',{name:'核对并保存配置',exact:true}).click();
  assert.ok((await page.getByRole('dialog').getByLabel('本部门运行数据目录').inputValue()).endsWith('order_matching'));
  await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();

  await page.getByRole('button',{name:'ERP调价监控',exact:true}).click();await section('查看价格变动');
  await page.getByRole('button',{name:'一键查看价格变动',exact:true}).click();
  assert.equal(await page.getByRole('dialog').count(),0,'一键查看不能再弹提交表单');
  await page.getByText('检测完成：1 个配件价格变化，0 个待核对。',{exact:true}).waitFor();
  await page.getByText('+1.00',{exact:true}).waitFor();await page.getByText('+2.00',{exact:true}).waitFor();
  await page.screenshot({path:path.join(output,'price-changes.png'),fullPage:true,animations:'disabled'});
  // 从旧入口定位到计划卡片，增加时间点并验证回读。
  const plan=id=>page.locator('#schedule-'+id);
  const shortcut=()=>page.getByRole('button',{name:'查看计划设置',exact:true}).click();
  await section('价格监控配置');
  assert.equal(await page.getByRole('button',{name:'选择并预览配置快照',exact:true}).count(),0);
  await shortcut();
  await plan('price').getByRole('button',{name:'设置调价检测计划',exact:true}).click();
  const schedule=page.getByRole('dialog');
  await schedule.getByRole('combobox',{name:'运行方式',exact:true}).click();
  await page.getByRole('option',{name:'每天固定时间',exact:true}).click();
  await schedule.getByLabel('时间 1',{exact:true}).fill('09:30');
  await schedule.getByRole('button',{name:'添加时间',exact:true}).click();
  await schedule.getByLabel('时间 2',{exact:true}).fill('15:45');
  await schedule.getByRole('button',{name:'保存计划',exact:true}).click();
  await plan('price').getByText('每天 09:30、15:45',{exact:true}).waitFor();
  await plan('price').getByRole('button',{name:'设置调价检测计划',exact:true}).click();
  assert.equal(await schedule.getByLabel('时间 2',{exact:true}).inputValue(),'15:45');
  await schedule.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('button',{name:'ERP调价监控',exact:true}).click();
  await section('价格监控配置');
  await page.getByRole('button',{name:'选择主机维护表',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'选择文件',exact:true}).first().waitFor();
  await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();

  await page.getByRole('button',{name:'管理监控商品',exact:true}).click();
  await page.getByRole('button',{name:'添加产品',exact:true}).click();
  let dialog=page.getByRole('dialog');
  assert.equal(await dialog.getByRole('button',{name:'添加',exact:true}).isDisabled(),true);
  await dialog.getByLabel('商品名称或 ERP ID').fill('ReUI 隔离测试');
  await dialog.getByRole('button',{name:'搜索',exact:true}).click();
  await dialog.getByRole('button',{name:'ReUI 隔离测试商品 · 12345',exact:true}).waitFor();
  await dialog.getByLabel('商品名称或 ERP ID').fill('12345');
  await dialog.getByRole('button',{name:'搜索',exact:true}).click();
  await dialog.getByRole('button',{name:'ReUI 隔离测试商品 · 12345',exact:true}).click();
  await dialog.getByLabel('预警阈值',{exact:true}).fill('7');await dialog.getByRole('button',{name:'添加',exact:true}).click();
  await page.getByText('产品配置已保存',{exact:true}).waitFor();
  // 顶部条件必须筛掉其他关注商品，并支持恢复列表。
  await page.getByRole('textbox',{name:'搜索商品或编号',exact:true}).fill('ReUI 隔离测试商品');
  await page.getByRole('button',{name:'应用筛选',exact:true}).click();
  await page.getByText(/当前筛选 1 \/ \d+ 件关注商品/).waitFor();
  assert.equal(await page.getByText('DEMO-0000',{exact:true}).count(),0);
  await page.getByRole('button',{name:'重置筛选',exact:true}).click();

  await page.getByRole('textbox',{name:'筛选已关注产品',exact:true}).fill('REUI-TEST');
  await page.getByRole('button',{name:'编辑产品',exact:true}).click();dialog=page.getByRole('dialog');
  assert.equal(await dialog.getByLabel('预警阈值',{exact:true}).inputValue(),'7');assert.equal(await dialog.getByLabel('SKU 编码',{exact:true}).getAttribute('readonly'),'');await dialog.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('button',{name:'批量移除关注',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByRole('checkbox',{name:'ReUI 隔离测试商品 · REUI-TEST',exact:true}).check();await dialog.getByRole('button',{name:'提交',exact:true}).click();
  await page.getByRole('button',{name:'确认移除关注',exact:true}).waitFor();await page.getByRole('button',{name:'确认移除关注',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'提交',exact:true}).click();await page.getByRole('alertdialog').getByRole('button',{name:'确认执行',exact:true}).click();await page.getByText('已移除 1 个关注产品',{exact:true}).waitFor();
  await page.getByRole('button',{name:'连接与通知设置',exact:true}).click();await section('库存数据来源');
  await shortcut();
  await plan('inventory').getByRole('button',{name:'设置库存监控周期',exact:true}).click();
  await page.getByRole('dialog').getByLabel('监控周期（分钟）',{exact:true}).fill('37');
  await page.getByRole('dialog').getByRole('button',{name:'保存周期',exact:true}).click();
  await plan('inventory').getByText('每 37 分钟',{exact:true}).waitFor();
  const shop=plan('shops').locator('li').filter({hasText:'英特尔旗舰店（样例）'});
  const other=plan('shops').locator('li').filter({hasText:'技嘉旗舰店（样例）'});
  const otherBefore=await other.innerText();
  await shop.getByRole('button',{name:'英特尔旗舰店（样例）：设置巡检周期',exact:true}).click();
  await page.getByRole('dialog').getByLabel('巡检周期（分钟）',{exact:true}).fill('47');
  await page.getByRole('dialog').getByRole('button',{name:'保存周期',exact:true}).click();
  await shop.getByText('每 47 分钟',{exact:true}).waitFor();
  assert.equal(await other.innerText(),otherBefore,'一家店铺的周期不应写到另一家店铺');
  await page.getByRole('button',{name:'连接与通知设置',exact:true}).click();
  await section('通知机器人');await page.getByRole('button',{name:'添加机器人',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('目标群名称',{exact:true}).waitFor();assert.equal(await dialog.getByLabel('目标群标识',{exact:true}).count(),0);assert.equal(await dialog.getByLabel('企业机器人编码',{exact:true}).count(),0);await dialog.getByRole('combobox',{name:'机器人类型',exact:true}).click();await page.getByRole('option',{name:'Webhook 机器人',exact:true}).click();assert.equal(await dialog.getByLabel('Webhook（留空保留原地址）',{exact:true}).getAttribute('type'),'password');assert.equal(await dialog.getByLabel('钉钉账号标识',{exact:true}).count(),0);await dialog.getByRole('button',{name:'取消',exact:true}).click();
  await section('本地缓存');await shortcut();
  await plan('cache').getByRole('button',{name:'设置定期清理',exact:true}).click();dialog=page.getByRole('dialog');
  await dialog.getByLabel('清理时间（北京时间）',{exact:true}).fill('04:15');await dialog.getByLabel('每隔多少天清理',{exact:true}).fill('2');await dialog.getByLabel('保留天数（0 为全部可清理缓存）',{exact:true}).fill('17');
  await dialog.getByRole('button',{name:'保存计划',exact:true}).click();
  await plan('cache').getByText('每 2 天 · 04:15',{exact:true}).waitFor();
  await plan('cache').getByRole('button',{name:'设置定期清理',exact:true}).click();dialog=page.getByRole('dialog');
  assert.equal(await dialog.getByLabel('清理时间（北京时间）',{exact:true}).inputValue(),'04:15');
  assert.equal(await dialog.getByLabel('保留天数（0 为全部可清理缓存）',{exact:true}).inputValue(),'17');
  await dialog.getByRole('button',{name:'取消',exact:true}).click();
  await plan('daily').getByRole('button',{name:'设置日报时间',exact:true}).click();dialog=page.getByRole('dialog');
  await dialog.getByLabel('时间 1',{exact:true}).fill('10:15');
  await dialog.getByRole('button',{name:'保存计划',exact:true}).click();
  await plan('daily').getByText('每天 10:15、16:00',{exact:true}).waitFor();
  // 发货监控独立显示；隔离工作区未绑定订单后端，不能触发真实巡检。
  await plan('shipping').locator('#schedule-title-shipping').waitFor();
  assert.equal(await plan('shipping').locator('#schedule-title-shipping').textContent(),'发货监控');
  assert.equal(await plan('shipping').getByText('尚未配置',{exact:true}).count(),1);
  assert.equal(await plan('shipping').getByRole('button',{name:'立即巡检',exact:true}).count(),0);
  assert.equal(await plan('orders').getByText('尚未配置',{exact:true}).count(),1);
  await page.getByRole('button',{name:'监控与巡检计划',exact:true}).click();
  await page.screenshot({path:path.join(output,'schedules.png'),fullPage:true,animations:'disabled'});
  await page.getByRole('button',{name:'切换深色',exact:true}).click();
  await page.screenshot({path:path.join(output,'schedules-dark.png'),fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:390,height:844});
  const scheduleWidth=await page.evaluate(()=>[document.documentElement.clientWidth,document.documentElement.scrollWidth]);
  assert.ok(scheduleWidth[1]<=scheduleWidth[0], '计划页面横向溢出 '+scheduleWidth);
  await page.screenshot({path:path.join(output,'schedules-mobile.png'),fullPage:true,animations:'disabled'});
  for(const [module,title,sections] of nav){
   await page.getByRole('button',{name:'展开或收起导航'}).click();await page.getByRole('button',{name:title,exact:true}).click();await page.locator(`[data-business-module="${module}"]`).waitFor();
   for(const title of sections){await section(title);const width=await page.evaluate(()=>[document.documentElement.clientWidth,document.documentElement.scrollWidth]);assert.ok(width[1]<=width[0],`${module}/${title} 页面横向溢出 ${width}`);}
   await page.screenshot({path:path.join(output,module+'-mobile.png'),fullPage:true,animations:'disabled'});
  }
  assert.deepEqual(errors,[]);assert.equal(page.context().pages().length,1);
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({passed:true,modules:nav.map(x=>x[0]),sections:nav.reduce((n,x)=>n+x[2].length,0),productCreateEditRemove:true,settingsSave:true,independentSchedules:true,passwordMasked:true,mobileWidth:390,errors},null,2));
  console.log(`ReUI 业务验收通过：${nav.length} 个导航入口、${nav.reduce((n,x)=>n+x[2].length,0)} 个分区、筛选重置、表单写入、深色与窄屏。`);
 }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
