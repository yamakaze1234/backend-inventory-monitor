import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { openDbx, jsonQuery } from './mcp-client.mjs';
import {safeError} from './daily-report-config.mjs';

export const ROOT = path.dirname(fileURLToPath(import.meta.url));
const GROUP = '抖英特旗舰组';
const SOURCE = '[视图运营].[单据利润]';
const FIELDS = ['出库时间','单据类型','出库单号','网店单号','店铺','项目组','整机数量','销售金额','处理器','显卡系列','机箱','销售类目'];
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => value == null ? '缺失' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const writeJson = (file, data) => fs.writeFile(file, JSON.stringify(data, null, 2), { flag: 'wx' });

export function dateRange(input) {
  const shanghaiToday = new Date(Date.now() + 8 * 3600000).toISOString().slice(0,10);
  const date = !input || input === 'yesterday' ? new Date(Date.parse(shanghaiToday + 'T00:00:00Z') - 86400000).toISOString().slice(0,10) : input;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || new Date(date + 'T00:00:00Z').toISOString().slice(0,10) !== date) throw new Error('日期必须是有效 YYYY-MM-DD 或 yesterday');
  if (date >= shanghaiToday) throw new Error('日报仅支持已结束的自然日');
  return { date, next: new Date(Date.parse(date + 'T00:00:00Z') + 86400000).toISOString().slice(0,10) };
}

export function aggregate(rows) {
  const group = new Map();
  for (const row of rows) {
    if (!['销售','退货'].includes(row.单据类型)) throw new Error(`未知单据类型：${row.单据类型}，停止生成`);
    if (row.项目组 !== GROUP) throw new Error('项目组越界，停止生成');
    for (const field of ['整机数量','销售金额']) if (row[field] != null && !Number.isFinite(row[field])) throw new Error(`${field} 不是有限数值`);
    if ((row.单据类型 === '退货' && (row.销售金额 > 0 || row.整机数量 > 0)) || (row.单据类型 === '销售' && (row.销售金额 < 0 || row.整机数量 < 0))) throw new Error('销售/退货符号异常，需要人工核对');
    const key = row.单据类型;
    const item = group.get(key) ?? { 单据类型:key, 明细行数:0, 整机数量:0, 销售金额:0, 缺失数量:0, 缺失金额:0 };
    item.明细行数++;
    if (row.整机数量 == null) item.缺失数量++; else item.整机数量 += row.整机数量;
    if (row.销售金额 == null) item.缺失金额++; else item.销售金额 += row.销售金额;
    group.set(key,item);
  }
  return ['销售','退货'].map(type => group.get(type) ?? {单据类型:type,明细行数:0,整机数量:0,销售金额:0,缺失数量:0,缺失金额:0});
}

function grouped(rows, keys) {
  const items = new Map();
  for (const r of rows) {
    const values = keys.map(k => r[k] || '未提供');
    const key = JSON.stringify(values);
    const item = items.get(key) ?? { values, salesQty:0, returnQty:0, netAmount:0, missingAmount:0 };
    if (r.单据类型 === '销售') item.salesQty += r.整机数量 ?? 0; else item.returnQty += Math.abs(r.整机数量 ?? 0);
    if (r.销售金额 == null) item.missingAmount++; else item.netAmount += r.销售金额;
    items.set(key,item);
  }
  return [...items.values()].sort((a,b) => b.netAmount - a.netAmount);
}

export function buildComboRankings(combos) {
  const rows = combos.map(r => ({cpu:r.values[0],gpu:r.values[1],salesQuantity:r.salesQty,returnQuantity:r.returnQty,netQuantity:r.salesQty-r.returnQty,netAmount:r.netAmount,missingAmountRows:r.missingAmount}));
  const nameOrder=(a,b)=>JSON.stringify([a.cpu,a.gpu]).localeCompare(JSON.stringify([b.cpu,b.gpu]),'zh-CN');
  const rank=items=>items.slice(0,10).map((r,index)=>({rank:index+1,...r}));
  return {
    basis:'整机净数量降序；相同数量按净销售金额降序；仍相同按型号名称排序',
    totalCombinations:rows.length,
    byNetQuantity:rank([...rows].sort((a,b)=>b.netQuantity-a.netQuantity || b.netAmount-a.netAmount || nameOrder(a,b))),
    byNetAmount:rank([...rows].sort((a,b)=>b.netAmount-a.netAmount || b.netQuantity-a.netQuantity || nameOrder(a,b))),
  };
}

export async function collectReport(dateInput, options = {}) {
  const {date,next} = dateRange(dateInput);
  const where = `[项目组]=N'${GROUP}' AND [出库时间]>='${date.replaceAll('-','')}' AND [出库时间]<'${next.replaceAll('-','')}'`;
  const controlSql = `SELECT (SELECT COUNT_BIG(*) AS row_count, COALESCE(SUM([整机数量]),0) AS quantity, COALESCE(SUM(CONVERT(decimal(20,4),[销售金额])),0) AS amount, COUNT_BIG(*)-COUNT([销售金额]) AS missing_amount, COUNT_BIG(*)-COUNT([整机数量]) AS missing_quantity FROM ${SOURCE} WHERE ${where} FOR JSON PATH,WITHOUT_ARRAY_WRAPPER) AS row_json`;
  const client = await (options.clientFactory || openDbx)(options.config);
  const started = new Date().toISOString();
  let rows = [], before, after;
  const queries = [controlSql];
  try {
    [before] = await jsonQuery(client, controlSql);
    if (!before || before.row_count > 100000) throw new Error('来源行数无效或超过 100000 行，需要拆分日期');
    const select = FIELDS.map(f => f === '出库时间' ? 'CONVERT(varchar(19),[出库时间],126) AS [出库时间]' : `[${f}]`).join(',');
    for (let offset=0;offset<before.row_count;offset+=100) {
      // Order by all selected fields: identical rows are interchangeable and are
      // retained, never deduplicated. Controls are checked again after paging.
      const sql = `SELECT (SELECT ${select} FOR JSON PATH,WITHOUT_ARRAY_WRAPPER,INCLUDE_NULL_VALUES) AS row_json FROM ${SOURCE} WHERE ${where} ORDER BY ${FIELDS.map(f=>'['+f+']').join(',')} OFFSET ${offset} ROWS FETCH NEXT 100 ROWS ONLY`;
      queries.push(sql);
      const page = await jsonQuery(client, sql);
      const expected = Math.min(100,before.row_count-offset);
      if (page.length !== expected) throw new Error(`分页不完整：${offset}，应有 ${expected} 行，实际 ${page.length}`);
      rows.push(...page);
    }
    [after] = await jsonQuery(client, controlSql);
  } finally { client.close(); }
  if (JSON.stringify(before) !== JSON.stringify(after)) throw new Error('查询期间来源总数/合计发生变化，请重新生成');
  const summary = aggregate(rows);
  const quantity = summary.reduce((n,r)=>n+r.整机数量,0);
  const amount = summary.reduce((n,r)=>n+r.销售金额,0);
  if (rows.length !== before.row_count || quantity !== before.quantity || Math.abs(amount-before.amount)>0.011 || summary.reduce((n,r)=>n+r.缺失金额,0)!==before.missing_amount || summary.reduce((n,r)=>n+r.缺失数量,0)!==before.missing_quantity) throw new Error('明细与数据库控制合计不一致');
  const dir = path.join(client.reportConfig.workspace, options.test ? 'daily-report-tests' : 'daily-reports',date,`${new Date().toISOString().replace(/[:.]/g,'-')}-${crypto.randomBytes(3).toString('hex')}`);
  await fs.mkdir(dir,{recursive:true});
  const payload = {date,next,group:GROUP,source:'ERP.视图运营.单据利润',timezone:'Asia/Shanghai',started,finished:new Date().toISOString(),control:before,summary,stores:grouped(rows,['店铺']),combos:grouped(rows.filter(r=>(r.整机数量??0)!==0),['处理器','显卡系列']),rows};
  const raw = JSON.stringify(rows);
  const manifest = {schemaVersion:1,date,group:GROUP,phase:'data_verified',dataVerified:true,checks:['100行分页完整','前后控制合计相同','本地明细与数据库总计一致'],rowCount:rows.length,rawSha256:crypto.createHash('sha256').update(raw).digest('hex'),control:before,queryStarted:started,queryFinished:payload.finished};
  await writeJson(path.join(dir,'source.json'),payload);
  await writeJson(path.join(dir,'runtime.json'),{python:client.reportConfig.python});
  await writeJson(path.join(dir,'manifest.json'),manifest);
  await fs.writeFile(path.join(dir,'queries.sql'),queries.join('\n\n'),{flag:'wx'});
  return {dir,payload,manifest};
}

function makeHtml(p) {
  const table = (heads, rows) => `<div class="scroll"><table><thead><tr>${heads.map(h=>`<th>${escapeHtml(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${escapeHtml(v ?? '缺失')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  const totals = p.summary;
  const net = totals.reduce((n,r)=>n+r.销售金额,0);
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${p.date} 抖英特出库日报</title><style>body{font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif;color:#1d1d1f;background:#f5f5f7;margin:0}main{max-width:1250px;margin:auto;padding:36px 24px}h1{font-size:30px;line-height:1.3}h2{font-size:21px;margin-top:32px}.muted{color:#62626b}.stats{display:flex;gap:16px;flex-wrap:wrap}.stat{background:white;padding:20px 24px;border-radius:12px;flex:1;min-width:180px}.stat strong{display:block;font-size:28px}.scroll{overflow:auto;background:white;border-radius:10px}table{border-collapse:collapse;width:100%;white-space:nowrap}td,th{padding:11px 14px;text-align:left;border-bottom:1px solid #eee}th{background:#e8eef7}input{padding:10px;border:1px solid #ccc;border-radius:7px;width:min(440px,85%)}details{margin-top:24px}footer{margin-top:28px;color:#62626b;font-size:13px}</style><main><p class="muted">${escapeHtml(p.group)} · 按出库时间统计</p><h1>${p.date} 出库日报</h1><p>统计区间：${p.date} 00:00 至 ${p.next} 00:00（北京时间，结束时间不包含）</p><div class="stats"><div class="stat">销售金额<strong>¥${money(totals[0].销售金额)}</strong></div><div class="stat">退货金额（绝对额）<strong>¥${money(Math.abs(totals[1].销售金额))}</strong></div><div class="stat">净销售金额<strong>¥${money(net)}</strong></div><div class="stat">整机净数量<strong>${p.control.quantity} 台</strong></div></div><h2>销售与退货</h2>${table(['单据类型','明细行数','整机数量（带符号）','销售金额（带符号）','金额缺失行数','数量缺失行数'],totals.map(r=>[r.单据类型,r.明细行数,r.整机数量,money(r.销售金额),r.缺失金额,r.缺失数量]))}<h2>店铺汇总</h2>${table(['店铺','销售整机数量','退货整机数量（绝对值）','净销售金额','金额缺失行数'],p.stores.map(r=>[...r.values,r.salesQty,r.returnQty,money(r.netAmount),r.missingAmount]))}<h2>CPU 与显卡组合</h2><p class="muted">仅包含整机数量不为零的行；型号按 ERP 原值分组，显卡缺失保留为“未提供”。</p>${table(['处理器','显卡系列','销售整机数量','退货整机数量（绝对值）','净销售金额','金额缺失行数'],p.combos.map(r=>[...r.values,r.salesQty,r.returnQty,money(r.netAmount),r.missingAmount]))}<h2>出库明细（${p.rows.length} 行）</h2><p><input id="search" placeholder="搜索订单号、店铺或配置" aria-label="搜索出库明细"></p><div id="detail">${table(FIELDS,p.rows.map(r=>FIELDS.map(f=>r[f])))}</div><details><summary>统计口径与完整性</summary><p>数据来源：${escapeHtml(p.source)}；项目组精确等于“${escapeHtml(p.group)}”。销售金额覆盖该组全部销售类目，整机数量按 ERP 字段求和；行数不等于订单数。退货保留来源负数，净额直接求和。NULL 金额和数量不计入求和，缺失行数单列；不将缺失值伪装为零。原始重复行保留。</p><p>完整性：已核对分页行数及前后数据库总数、数量、金额；这些控制项相同不能排除查询期间等额替换。生成时间：${escapeHtml(new Date(p.finished).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}))}（北京时间）。</p></details><footer>Excel 保留可筛选明细；本页面全部资源内嵌，可离线搜索。</footer></main><script>document.getElementById('search').addEventListener('input',e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('#detail tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))})</script></html>`;
}

export async function renderReport(dir,p) {
  // 使用软件已锁定的 openpyxl，不把本机专有渲染依赖复制进发行包。
  const { spawnSync } = await import('node:child_process');
  const config = JSON.parse(await fs.readFile(path.join(dir,'runtime.json'),'utf8'));
  const rendered = spawnSync(config.python, [path.join(ROOT,'daily_report_excel.py'), dir], {windowsHide:true, encoding:'utf8',timeout:120000});
  if (rendered.error || rendered.status !== 0) throw new Error('文件生成失败：请检查 Python 与 openpyxl 依赖');
  const prefix=`抖英特出库日报-${p.date}`;
  await fs.writeFile(path.join(dir,prefix+'.html'),makeHtml(p),{flag:'wx'});
  const sales=p.summary[0],returns=p.summary[1];
  const cpuGpuRankings=buildComboRankings(p.combos);
  const rankingText=cpuGpuRankings.byNetQuantity.map(r=>`${r.rank}. ${r.cpu} / ${r.gpu}：销售 ${r.salesQuantity} 台，退货 ${r.returnQuantity} 台，净 ${r.netQuantity} 台，净额 ¥${money(r.netAmount)}${r.missingAmountRows?`（金额缺失 ${r.missingAmountRows} 行）`:''}`).join('\n') || '该日期没有整机组合数据。';
  const summary=`${p.date} 抖英特旗舰组出库日报\n销售金额：¥${money(sales.销售金额)}\n退货金额（绝对额）：¥${money(Math.abs(returns.销售金额))}\n净销售金额：¥${money(p.control.amount)}\n销售整机：${sales.整机数量} 台；退货整机：${Math.abs(returns.整机数量)} 台；整机净数量：${p.control.quantity} 台\n明细：${p.rows.length} 行（不是订单数）\n口径：出库时间，北京时间自然日；项目组精确筛选，金额含全部销售类目，退货带负号。\n金额缺失 ${p.control.missing_amount} 行，数量缺失 ${p.control.missing_quantity} 行。\n\nCPU 与显卡组合前 10（${cpuGpuRankings.basis}；共 ${cpuGpuRankings.totalCombinations} 组）：\n${rankingText}\n仅含整机数量非零行，配置按 ERP 原值分组，显卡缺失为“未提供”。\nExcel、HTML 已生成。`;
  await fs.writeFile(path.join(dir,'钉钉摘要.txt'),summary,{flag:'wx'});
  const result={complete:true,date:p.date,group:p.group,rowCount:p.rows.length,summary,cpuGpuRankings,files:[prefix+'.xlsx',prefix+'.html','钉钉摘要.txt'].map(f=>path.join(dir,f))};
  await writeJson(path.join(dir,'result.json'),result);
  return result;
}

if(process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args=process.argv.slice(2);
    if(args.length>1) throw new Error('用法：node report.mjs [YYYY-MM-DD|yesterday]');
    const {dir,payload}=await collectReport(args[0]);
    console.error(`已核对 ${payload.rows.length} 行，正在生成 Excel 和 HTML`);
    console.log(JSON.stringify(await renderReport(dir,payload),null,2));
  } catch(e) {console.error(JSON.stringify({complete:false,error:safeError(e)}));process.exitCode=1;}
}
