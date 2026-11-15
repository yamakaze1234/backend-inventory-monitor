import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

// 每个任务读取一次完整配置；工作目录和旧连接名均不能作为兜底。
export async function loadConfig() {
  try {
    let pointer=process.env.ORDER_BOT_CONFIG;
    if (!pointer) pointer=JSON.parse(await fs.readFile(new URL('./order-department.json',import.meta.url),'utf8')).configPath;
    if (!path.isAbsolute(pointer)) throw new Error();
    const workspace=path.dirname(pointer);
    const config=JSON.parse(await fs.readFile(path.join(workspace,'daily-report.json'),'utf8'));
    if(config.version!==1 || !config.connectionId?.trim() || config.networkMode!=='direct' || !config.connectionName?.trim() || config.database!=='ERP') throw new Error();
    for(const key of ['dbxExecutable','dbxData','python']) if(!path.isAbsolute(config[key]||'')) throw new Error();
    return Object.freeze({...config,workspace});
  } catch {throw new Error('日报配置无效或尚未配置，请在订单机器人设置中重新绑定连接');}
}

export function directEnvironment(config) {
  // 只影响本工具的子进程，不改变系统 VPN 或代理设置。
  const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>!['http_proxy','https_proxy','all_proxy','no_proxy','node_use_env_proxy'].includes(k.toLowerCase())));
  return {...env,NO_PROXY:'*',DBX_DATA_DIR:config.dbxData,DBX_MCP_ALLOW_WRITES:'false',DBX_MCP_SCOPE_CONNECTION_NAME:config.connectionName,DBX_MCP_SCOPE_DATABASE:config.database};
}

export function safeError(error) {
  const text=String(error?.message||error);
  // 服务端原始错误可能包含连接串，界面只返回受控分类。
  if(/permission|denied|229/i.test(text))return '视图读取权限不足，请联系数据库管理员';
  if(/login|18456/i.test(text))return '数据库登录失败，请在 DBX 检查账号';
  if(/spawn|ENOENT|EPERM|exited/i.test(text))return '子进程启动或运行失败，请检查 DBX 和运行环境';
  if(/timeout|I\/O|buffer|connect|network/i.test(text))return '数据库网络连接失败，请检查内网地址及服务';
  if(/文件生成/.test(text))return '文件生成失败，请检查输出目录及 Python 依赖';
  if(/配置|连接不存在/.test(text))return '日报配置或连接失效，请重新绑定';
  return '日报校验失败，未生成成功报告；请检查日期、分页及数据完整性';
}
