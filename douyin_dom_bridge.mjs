// 页面巡检专用桥接：只允许展开、翻页和重置筛选，不提供库存写入入口。
import { homedir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const input = [];
for await (const chunk of process.stdin) input.push(chunk);
let stage = '连接浏览器';
try {
  const args = JSON.parse(Buffer.concat(input).toString('utf8'));
  const { Page } = await import(pathToFileURL(join(homedir(), '.opencli/node_modules/@jackwener/opencli/dist/src/browser/page.js')));
  const page = new Page(args.session);
  page._page = args.page;
  stage = '核对店铺';
  // 筛选会追加查询参数，只核对固定站点与页面路径，不读取或记录参数内容。
  const identity = await page.evaluate(`({url:location.origin+location.pathname,shop:document.querySelector('[class*="index_userName"]')?.textContent?.trim()})`);
  const exporting = args.action === 'export_click';
  const expectedUrls = exporting ? ['https://fxg.jinritemai.com/ffa/g/list', 'https://fxg.jinritemai.com/ffa/g/excel'] :
    ['https://fxg.jinritemai.com/ffa/morder/stock-manage/list'];
  if (!expectedUrls.includes(identity.url) || identity.shop !== args.shop) {
    throw new Error('店铺身份或库存页面不一致');
  }
  let selector;
  let options = {};
  let performed = false;
  if (exporting) {
    // 官方导出动作限定页面、选择器和文案；不开放上架、改价、改库存或任意点击。
    const allowed = identity.url.endsWith('/g/list') ? [
      ['[role="tab"]', '全部'], ['button[type="reset"]', '重置'],
      ['#exportSearchedGoods', '导出查询商品'], ['button.ecom-g-btn-primary', '导出'],
    ] : [];
    const download = identity.url.endsWith('/g/excel') &&
      /^tr\[data-row-key="[0-9]+"\] a$/.test(args.selector) && args.text === '下载报表';
    if (!download && !allowed.some(([s,t]) => s === args.selector && t === args.text)) throw Error('不允许的导出动作');
    selector = args.selector;
    const matches = await page.evaluate(`([...document.querySelectorAll(${JSON.stringify(selector)})].flatMap((e,i)=>
      e.getClientRects().length && e.innerText.trim()===${JSON.stringify(args.text)} ? [i] : []))`);
    if (matches.length !== 1) throw Error('导出控件不唯一');
    options = {nth:matches[0]};
  } else if (args.action === 'expand' && /^[0-9]+$/.test(args.product)) {
    selector = `tr[data-row-key="product-${args.product}"] .auxo-table-row-expand-icon-collapsed`;
  } else if (args.action === 'next') {
    selector = 'li[title="下一页"][aria-disabled="false"]';
  } else if (args.action === 'reset') {
    const found = await page.evaluate(`([...document.querySelectorAll('button')].filter(e=>e.innerText.trim()==='重置').map(e=>({type:e.type})))`);
    if (found.length !== 1 || found[0].type !== 'reset') throw new Error('重置按钮不唯一');
    selector = 'button[type="reset"]';
  } else if (args.action === 'category') {
    // 根据可见“商品类目”字段定位下拉框，不依赖固定输入框序号。
    stage = '定位类目';
    const ids = await page.evaluate(`([...document.querySelectorAll('form .auxo-form-item')].filter(e=>e.innerText.startsWith('商品类目')).map(e=>e.querySelector('input')?.id))`);
    if (ids.length !== 1 || !/^rc_select_[0-9]+$/.test(ids[0])) throw new Error('商品类目字段不唯一');
    selector = `form .auxo-form-item:has(#${ids[0]}) .auxo-select-selector`;
    // 此下拉框监听 mousedown；普通 click 不会展开，只对该已核验表单字段派发鼠标事件。
    stage = '展开类目';
    await page.evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)throw Error('类目框消失');e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0}));return true})()`);
    // 让 Node 自然结束，避免 Windows 下强制退出导致 libuv 句柄断言。
    performed = true;
  } else if (args.action === 'category_item' && ['3C数码及配件','电脑','DIY电脑','DIY整机','DIY兼容机'].includes(args.product)) {
    selector = '.auxo-select-dropdown [class*="listDataName"]';
    const names = await page.evaluate(`([...document.querySelectorAll(${JSON.stringify(selector)})].map(e=>e.innerText.trim()))`);
    const indexes = names.flatMap((name,index)=>name===args.product?[index]:[]);
    if (indexes.length !== 1) throw new Error('整机类目选项不唯一');
    options = {nth:indexes[0]};
  } else if (args.action === 'search') {
    const found = await page.evaluate(`([...document.querySelectorAll('form button')].filter(e=>e.innerText.trim()==='查询').map(e=>e.type))`);
    if (found.length !== 1 || found[0] !== 'submit') throw new Error('查询按钮不唯一');
    selector = 'form button[type="submit"]';
  } else throw new Error('不支持的巡检动作');
  // 当前桥接原生坐标点击会空回执；使用同一 SDK 的 DOM 点击回退并在 Python 侧核验状态。
  page.tryNativeClick = async () => false;
  page.tryCdpOnResolvedElement = async () => false;
  if (!performed) await page.click(selector, options);
  process.stdout.write(JSON.stringify({ok:true}));
} catch {
  // 不把可能含连接参数的底层错误写入日志。
  process.stdout.write(JSON.stringify({ok:false,stage,error:'页面巡检操作失败，请核对店铺与页面'}));
  process.exitCode = 1;
}
