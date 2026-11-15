import readline from 'node:readline';
import {collectReport,renderReport} from './report.mjs';
import {safeError} from './daily-report-config.mjs';

// MCP 只暴露固定日报，不提供任意 SQL 或消息发送接口。
console.log=(...args)=>console.error(...args);
const send=x=>process.stdout.write(JSON.stringify({jsonrpc:'2.0',...x})+'\n');
const tool={name:'generate_daily_report',description:'生成已结束自然日的固定项目组日报，不发送消息。',inputSchema:{type:'object',properties:{date:{type:'string'}},additionalProperties:false}};
let queue=Promise.resolve();
async function handle(msg){
  if(msg.id==null)return;
  try{
    let result;
    if(msg.method==='initialize')result={protocolVersion:'2024-11-05',capabilities:{tools:{}},serverInfo:{name:'readonly-daily-report',version:'2.0'}};
    else if(msg.method==='tools/list')result={tools:[tool]};
    else if(msg.method==='ping')result={};
    else if(msg.method==='tools/call'){
      const args=msg.params?.arguments||{};
      if(msg.params?.name!==tool.name || Object.keys(args).some(k=>k!=='date') || (args.date!==undefined && typeof args.date!=='string'))throw new Error('配置参数无效');
      const {dir,payload}=await collectReport(args.date);
      const value=await renderReport(dir,payload);
      result={content:[{type:'text',text:JSON.stringify(value)}],structuredContent:value,isError:false};
    }else throw new Error('配置方法无效');
    send({id:msg.id,result});
  }catch(error){send({id:msg.id,result:{isError:true,content:[{type:'text',text:JSON.stringify({complete:false,error:safeError(error)})}]}});}
}
readline.createInterface({input:process.stdin}).on('line',line=>{
  let msg;try{msg=JSON.parse(line);}catch{return;}
  queue=queue.then(()=>handle(msg));
});
