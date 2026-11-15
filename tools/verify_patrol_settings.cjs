// 在候选隔离预览中验收真实设置表单；不读取生产配置、不发送群消息。
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || '../app/web/node_modules/playwright');
async function main() {
  const url=process.argv[2];
  if(!url?.startsWith('http://127.0.0.1:'))throw Error('需要隔离预览地址');
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(url);
    await page.getByRole('button',{name:'监控与巡检计划',exact:true}).click();
    const plan=page.locator('#schedule-inventory');
    await plan.getByRole('button',{name:'设置运行方式',exact:true}).click();
    let dialog=page.getByRole('dialog');
    await dialog.getByRole('combobox',{name:'运行方式',exact:true}).click();
    await page.getByRole('option',{name:'每天固定时间点',exact:true}).click();
    await dialog.getByLabel('时间 1',{exact:true}).fill('09:30');
    await dialog.getByRole('button',{name:'添加时间',exact:true}).click();
    await dialog.getByLabel('时间 2',{exact:true}).fill('15:45');
    await dialog.getByRole('button',{name:'保存计划',exact:true}).click();
    await plan.getByText('每天 09:30、15:45（北京时间）',{exact:true}).waitFor();
    await page.reload();
    await page.getByRole('button',{name:'监控与巡检计划',exact:true}).click();
    await plan.getByRole('button',{name:'设置运行方式',exact:true}).click();
    dialog=page.getByRole('dialog');
    assert.equal(await dialog.getByLabel('时间 2',{exact:true}).inputValue(),'15:45');
    await dialog.getByRole('combobox',{name:'运行方式',exact:true}).click();
    await page.getByRole('option',{name:'按间隔运行',exact:true}).click();
    await dialog.getByRole('button',{name:'保存计划',exact:true}).click();
    await plan.getByText(/每 \d+ 分钟/).first().waitFor();
    // 三个店铺都有独立运行方式入口，不能只给总计划增加开关。
    assert.equal(await page.locator('#schedule-shops').getByRole('button',{name:/设置运行方式/}).count(),3);
    await page.getByRole('button',{name:'环境配置',exact:true}).click();
    assert.equal(await page.getByRole('dialog').count(),0);
    await page.getByRole('button',{name:'添加机器人',exact:true}).click();
    dialog=page.getByRole('dialog');
    for(const label of ['发货监控提醒','订单巡检结果','前台库存巡检失败提醒','抖店补库存提醒']) {
      await dialog.getByRole('checkbox',{name:label,exact:true}).waitFor();
    }
    await dialog.getByRole('button',{name:'取消',exact:true}).click();
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,tests:6,productionDataUsed:false,messagesSent:false}));
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
