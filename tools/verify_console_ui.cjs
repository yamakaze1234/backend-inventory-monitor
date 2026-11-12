// 隔离浏览器验收：只操作模拟队列，截图保存在输出目录，不打开 Codex 预览。
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
async function main() {
  const url = process.argv[2];
  if (!url?.startsWith('http://127.0.0.1:')) throw Error('请提供隔离预览地址');
  const out = path.resolve('outputs/订单整合验收');
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ channel:'chrome', headless:true });
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', e=>errors.push(e.message));
    // 界面验收不访问发布站；真实签名与下载失败由后端测试覆盖。
    await page.route('**/api/info',route=>route.fulfill({json:{version:'1.06',autoCheck:false,business:true}}));
    await page.goto(url);
    assert.deepEqual(await page.locator('[data-slot=sidebar-menu-button] span').allTextContents(),['配件库存监控','订单巡检匹配','前台库存监控','钉钉货源监控','ERP调价监控','库存提醒记录','监控与巡检计划','管理监控商品','连接与通知设置','订单机器人设置','软件更新']);
    const icon=await page.request.get(new URL('/app-icon.ico',url).href);
    assert.equal(icon.status(),200);
    assert.deepEqual(await icon.body(),fs.readFileSync('app/ui/monitor_assets/monitor.ico'));
    const updateLink=page.locator('[data-slot="sidebar-footer"]').getByRole('button',{name:'软件更新',exact:true});
    await updateLink.waitFor();
    const updateBox=await updateLink.boundingBox();
    assert.ok(updateBox.y>800&&updateBox.x<260,'更新入口固定在桌面左下角');
    const nav=[['douyin','前台库存监控'],['dingtalk','钉钉货源监控'],['profit','ERP调价监控'],['events','库存提醒记录'],['monitor','管理监控商品'],['settings','连接与通知设置']];
    for(const [module,title] of nav){
      await page.getByRole('button',{name:title,exact:true}).click();
      await page.locator(`[data-business-module="${module}"]`).waitFor();
      await page.getByRole('combobox',{name:'业务分区'}).waitFor();
      assert.equal(await page.locator('[data-native-module]').count(),0);
      assert.equal(await page.getByRole('alert').count(),0);
      assert.equal(await page.getByRole('heading',{level:1}).innerText(),title);
      const box=await page.locator(`[data-business-module="${module}"]`).boundingBox();
      assert.ok(box.x>=256&&box.width>900,'业务页留在右侧内容区');
    }
    const modules=nav.map(item=>item[0]);
    assert.equal(page.context().pages().length,1,'切换业务页不得打开新页面');
    await page.getByRole('button',{name:'配件库存监控',exact:true}).click();


    await page.getByText('1–25 / 共 120 条').waitFor();
    assert.equal(await page.locator('tbody tr').count(),25);
    await page.screenshot({path:path.join(out,'inventory.png'),fullPage:true});
    await page.getByPlaceholder('输入商品名称或 SKU').fill('DEMO-0001');
    await page.getByText('1–1 / 共 1 条').waitFor();
    await page.getByRole('button',{name:'订单巡检匹配',exact:true}).click();
    await page.getByText('1–25 / 共 83 条').waitFor();
    await page.screenshot({path:path.join(out,'orders.png'),fullPage:true});
    await page.getByRole('button',{name:'下一页',exact:true}).click();
    await page.getByText('26–50 / 共 83 条').waitFor();
    await page.locator('tbody button').first().click();
    await page.getByRole('dialog').waitFor();
    await page.getByText('隔离样例结果：本次验收没有执行 ERP 写入或发送群消息。').waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({state:'hidden'});
    await page.getByRole('button',{name:'切换深色'}).click();
    await page.screenshot({path:path.join(out,'orders-dark.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
    const dimensions=await page.evaluate(()=>({client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}));
    assert.ok(dimensions.scroll<=dimensions.client,JSON.stringify(dimensions));
    // 按当前网店单号字段的可访问名称验证，避免旧提示文案造成误报。
    const inputBox=await page.getByRole('textbox',{name:'完整网店单号',exact:true}).boundingBox();
    assert.ok(inputBox.width>250,'窄窗口的订单输入框必须保持可用宽度');
    await page.getByRole('button',{name:'展开或收起导航'}).click();
    await page.getByRole('button',{name:'软件更新',exact:true}).click();
    await page.getByRole('button',{name:'检查更新',exact:true}).waitFor();
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'browser-verification.json'),JSON.stringify({passed:true,rowsPerPage:25,orders:83,mobile:dimensions,reuiBusinessModules:modules,iconMatches:true,updateInFooter:true,errors},null,2));
    console.log('浏览器验证通过：分页、搜索、详情、深色、390px 布局及更新入口。');
  } finally { await browser.close(); }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
