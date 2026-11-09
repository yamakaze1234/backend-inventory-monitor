import readline from 'node:readline';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {readFileSync,existsSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
const exec = promisify(execFile);
// 子工具从同一个公开部门配置获取运行目录和店铺，不能回退到个人路径。
const configPath=process.env.ORDER_BOT_CONFIG || JSON.parse(readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)),'order-department.json'),'utf8')).configPath;
const department=JSON.parse(readFileSync(configPath,'utf8'));
process.env.ORDER_BOT_CONFIG=configPath;
const script=path.join(department.match_root,'app/src/group_order_bot.py');
const shopIds=department.shops.map(s=>s.id);
function matchingPython(){
  if(!existsSync(department.python_path))throw new Error('匹配运行环境不存在');
  return department.python_path;
}
const tools = [
  {name:'match_order',description:'收到明确的匹配订单请求时调用。默认使用完整网店单号，保留 A 或 A-1 后缀；只有用户明确提供 NO- 开头的 ERP 单号才使用备用入口。无任务指令的裸数字用 match_number。查最近四个自然日，仅限本部门配置的店铺。校验维护表和 ERP 后才匹配，通知默认显示网店单号。queued 仅表示入队。',inputSchema:{type:'object',properties:{number:{type:'string',description:'完整网店单号；明确使用 ERP 单号时保留 NO- 前缀。'},shopId:{type:'string',enum:shopIds}},required:['number'],additionalProperties:false}},
  ...[
    ['rematch_order','用户明确说重新匹配订单时调用。默认用完整网店单号并保留 A 或 A-1 后缀，明确给出 NO- 开头的 ERP 单号时也支持。人工已核验并保存维护表，授权更新原套餐配件；引用该套餐的其他订单也可能受影响。'],
    ['match_spu','匹配SPU：严格按维护表中的SKU处理整商品全部配置。异常跳过并汇总，不覆盖套餐差异。'],
    ['rematch_spu','用户明确说重新匹配SPU时调用。核对该SPU所有配置，授权按已保存维护表更新原套餐配件；引用该套餐的其他订单也可能受影响。'],
    ['match_number','用户只发送完整数字号码时调用。后端核对维护表SPU与订单，无法唯一判断则要求明确类型；仅普通匹配，不覆盖差异。'],
  ].map(([name,description])=>({name,description:description+' queued仅表示入队，不是完成。',inputSchema:{type:'object',properties:{number:{type:'string'},shopId:{type:'string',enum:shopIds}},required:['number'],additionalProperties:false}})),
  {name:'patrol_today',description:'立即对北京时间昨天零点到当前的 ERP 未匹配订单执行一轮巡检，可确定的自动匹配，异常按网店单号汇总到设置中的目标群。任务异步执行，返回任务编号。',inputSchema:{type:'object',properties:{shopId:{type:'string',enum:shopIds}},additionalProperties:false}},
  {name:'matching_status',description:'查询订单匹配后台及最近任务；有jobId则查该任务。向用户原样输出结果顶层replyText，它是固定巡检格式。禁止把扫描商品行数改称待处理订单数。',inputSchema:{type:'object',properties:{jobId:{type:'string',pattern:'^[a-f0-9]{32}$'}},additionalProperties:false}},
].map(t=>({...t,annotations:{readOnlyHint:t.name==='matching_status',destructiveHint:t.name!=='matching_status',idempotentHint:true,openWorldHint:true}}));
export function matchCommand(name, number){
  const kind={match_order:'order',rematch_order:'rematch_order',match_spu:'spu',rematch_spu:'rematch_spu',match_number:'number'}[name];
  if(!kind)throw new Error('匹配工具无效');
  if(typeof number!=='string')throw new Error('订单号或SPU必须为字符串');
  number=number.trim().toUpperCase();
  if(['order','rematch_order'].includes(kind)){
    if(!/^(?:[1-9][0-9]{14,24}(?:A(?:-[0-9]+)?)?|NO-[A-Za-z0-9-]{4,70})$/.test(number))
      throw new Error('请提供完整网店单号（保留 A 或 A-1 后缀）；也可明确使用 NO- 开头的 ERP 单号');
  }else if(!/^[1-9][0-9]{14,24}$/.test(number))throw new Error('请提供完整数字SPU或号码');
  return ['submit','--kind',kind,'--number',number];
}
const send = data => process.stdout.write(JSON.stringify({jsonrpc:'2.0',...data})+'\n');
async function handle(msg){
  if(msg.id==null)return;
  try{
    let result;
    if(msg.method==='initialize')result={protocolVersion:'2024-11-05',capabilities:{tools:{}},serverInfo:{name:'erp-order-matching',version:'1.2.1'}};
    else if(msg.method==='tools/list')result={tools};
    else if(msg.method==='ping')result={};
    else if(msg.method==='tools/call'){
      const name=msg.params?.name,args=msg.params?.arguments??{};
      const tool=tools.find(t=>t.name===name);
      if(!tool || typeof args!=='object' || Array.isArray(args) || Object.keys(args).some(k=>!(k in tool.inputSchema.properties)))throw new Error('参数无效');
      if(args.shopId!==undefined&&!shopIds.includes(args.shopId))throw new Error('店铺无效');
      let command;
      if(['match_order','rematch_order','match_spu','rematch_spu','match_number'].includes(name)){
        command=matchCommand(name,args.number);
      }else if(name==='patrol_today')command=['submit','--kind','today'];
      else{
        if(args.jobId!==undefined&&(typeof args.jobId!=='string'||!/^[a-f0-9]{32}$/.test(args.jobId)))throw new Error('任务编号无效');
        command=['status',...(args.jobId?['--job',args.jobId]:[])];
      }
      if(args.shopId)command.push('--shop',args.shopId);
      const {stdout}=await exec(matchingPython(),['-B','-X','utf8',script,...command],{windowsHide:true,timeout:30000,maxBuffer:1024*1024});
      const value=JSON.parse(stdout);
      result={content:[{type:'text',text:JSON.stringify(value)}],structuredContent:value,isError:false};
    }else{send({id:msg.id,error:{code:-32601,message:'Method not found'}});return;}
    send({id:msg.id,result});
  }catch(error){send({id:msg.id,result:{isError:true,content:[{type:'text',text:error.killed?'匹配队列响应超时，请查询任务状态，勿重复提交':error.message.slice(0,500)}]}});}
}
if(process.argv[1] && import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
readline.createInterface({input:process.stdin}).on('line',line=>{
  let msg;try{msg=JSON.parse(line);}catch{return;}
  handle(msg);
});
