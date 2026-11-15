import {loadConfig,safeError,directEnvironment} from './daily-report-config.mjs';
import {openDbx,jsonQuery,RpcClient,connectionMetadata} from './mcp-client.mjs';
import {collectReport,renderReport} from './report.mjs';

// 桌面诊断只走固定命令；测试报告输出与正式日报目录分离。
try {
  const [action,date]=process.argv.slice(2);
  if(action==='discover'){
    let input='';for await(const part of process.stdin)input+=part;
    const config=JSON.parse(input);
    const c=new RpcClient(config.dbxExecutable,[],{env:directEnvironment({...config,connectionName:'',database:'ERP'})});
    try{await c.request('initialize',{protocolVersion:'2024-11-05',capabilities:{},clientInfo:{name:'report-settings',version:'1'}});c.notify('notifications/initialized');console.log(JSON.stringify({ok:true,connections:await connectionMetadata(c)}));}finally{c.close();}
  }else if(action==='test'){
    const client=await openDbx();
    try {
      await jsonQuery(client,"SELECT (SELECT COUNT_BIG(*) AS verified FROM (SELECT TOP (0) [项目组] FROM [视图运营].[单据利润] WHERE [项目组]=N'抖英特旗舰组') AS probe FOR JSON PATH,WITHOUT_ARRAY_WRAPPER) AS row_json");
      console.log(JSON.stringify({ok:true,message:'数据库登录及日报视图读取成功'}));
    }finally{client.close();}
  }else if(action==='report'){
    const {dir,payload}=await collectReport(date,{test:true});
    const result=await renderReport(dir,payload);
    console.log(JSON.stringify({ok:true,message:'测试日报已生成，未发送消息',...result}));
  }else throw new Error('配置操作无效');
} catch(error){console.log(JSON.stringify({ok:false,message:safeError(error)}));process.exitCode=1;}
