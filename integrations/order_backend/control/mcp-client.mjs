import {loadConfig,directEnvironment} from './daily-report-config.mjs';
import { spawn } from 'node:child_process';
import readline from 'node:readline';

// A newline-delimited JSON-RPC client for local stdio servers. No credentials
// are read or printed: DBX loads the existing authorized local connection.
export class RpcClient {
  constructor(command, args = [], options = {}) {
    this.seq = 0;
    this.pending = new Map();
    this.child = spawn(command, args, { ...options, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    this.stderr = '';
    this.child.stderr.on('data', chunk => { this.stderr = (this.stderr + chunk).slice(-12000); });
    readline.createInterface({ input: this.child.stdout }).on('line', line => {
      let msg;
      try { msg = JSON.parse(line); } catch { return; }
      if (msg.id != null && (msg.result !== undefined || msg.error)) {
        const pending = this.pending.get(msg.id);
        if (!pending) return;
        clearTimeout(pending.timer);
        this.pending.delete(msg.id);
        msg.error ? pending.reject(new Error(JSON.stringify(msg.error))) : pending.resolve(msg.result);
      } else if (msg.id != null) {
        // Never silently grant a server-requested approval.
        this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, error: { code: -32601, message: 'Interactive approval unavailable in report script' } }) + '\n');
      }
      this.onNotification?.(msg);
    });
    const rejectAll = error => {
      for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(error); }
      this.pending.clear();
    };
    this.child.on('error', error => {this.failure=error;rejectAll(error);});
    this.child.on('exit', code => {this.failure=new Error(`RPC process exited (${code})`);rejectAll(this.failure);});
  }
  request(method, params = {}, timeout = 90000) {
    if(this.failure)return Promise.reject(this.failure);
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`RPC timeout: ${method}`)); }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }
  notify(method, params) {
    this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, ...(params === undefined ? {} : { params }) }) + '\n');
  }
  close() { this.child.stdin.end(); this.child.kill(); }
}

export async function openDbx(config) {
  config=config || await loadConfig();
  const client = new RpcClient(config.dbxExecutable, [], {env:directEnvironment(config)});
  client.reportConfig=config;
  try { await client.request('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'readonly-daily-report', version: '1.0.0' } });
  client.notify('notifications/initialized');
  const rows=await connectionMetadata(client);
  if(!rows.some(r=>r.id===config.connectionId && r.name===config.connectionName))throw new Error('连接不存在或已重命名，请重新绑定');
  return client; } catch(error) {client.close();throw error;}
}

export async function jsonQuery(client, sql) {
  if (!/^SELECT\s/i.test(sql) || /;|--|\/\*/.test(sql)) throw new Error('Only a single fixed SELECT is allowed');
  const result = await client.request('tools/call', { name: 'dbx_execute_query', arguments: { connection_id: client.reportConfig.connectionId, connection_name: client.reportConfig.connectionName, database: client.reportConfig.database, sql, cell_char_limit: 4000 } });
  const text = result.content?.filter(c => c.type === 'text').map(c => c.text).join('\n') ?? '';
  if (result.isError) throw new Error(text);
  const rows = [];
  for (const line of text.split('\n')) {
    if (!line.startsWith('| {') || !line.endsWith(' |')) continue;
    // One JSON-valued column: internal pipes belong to the JSON, not columns.
    const encoded = line.slice(2, -2).replace(/\\\|/g, '|');
    try { rows.push(JSON.parse(encoded)); }
    catch { throw new Error('DBX JSON cell is truncated or malformed; report stopped'); }
  }
  if (!rows.length && !/\(0 rows|No rows/i.test(text)) throw new Error('Unexpected DBX response; no verified JSON rows');
  return rows;
}

// DBX 正常元数据接口只取公开列，不读取本机凭据数据库。
export async function connectionMetadata(client) {
  const result=await client.request('tools/call',{name:'dbx_list_connections',arguments:{}});
  if(result.isError)throw new Error('配置连接读取失败');
  const text=result.content?.filter(c=>c.type==='text').map(c=>c.text).join('\n')||'';
  const lines=text.split('\n').filter(l=>l.startsWith('|'));
  if(!lines.length)return [];
  const cells=l=>l.split('|').slice(1,-1).map(v=>v.trim());
  if(JSON.stringify(cells(lines[0]))!==JSON.stringify(['ID','Name','Group Path','Type','Host','Port','Database']))throw new Error('配置元数据格式不支持');
  return lines.slice(2).map(line=>{const [id,name,,type,host,port,database]=cells(line);return {id,name,type,host,port,database};}).filter(r=>r.id&&r.type==='sqlserver');
}
