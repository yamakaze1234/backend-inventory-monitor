import { spawn } from 'node:child_process';
import { readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

const fixed = new Set('帮助 查询商品 查库存 查价格变化 添加监控 删除监控 修改监控阈值 查订单状态 匹配订单 巡检订单 查看任务进度 发货监控汇总 前台库存监控'.split(' '));
const natural = /^(?:查询|帮我查(?:询|一下)?|查一下|查)\s*([^;。?!]+?)\s*(?:的\s*)?(?:库存(?:\s*(?:和|及|与|、|以及|\/)\s*ERP\s*(?:库存成本|成本价|价格|单价|成本))?|ERP\s*(?:库存成本|成本价|价格|单价|成本))[?。]?$/iu;

export function isFixed(text) {
    // 保留已知指令的错误参数处理；识别普通库存短句时与 Python 解析器对齐。
    const clean = String(text ?? '').trim().replace(/\s+/gu, ' ');
    return fixed.has(clean.split(' ', 1)[0]) || natural.test(clean.normalize('NFKC'));
}

export function invoke(event, state) {
    return new Promise(resolve => {
        const child = spawn(process.env.STOCK_OPS_PYTHON,
            ['-I', '-B', '-X', 'utf8', path.join(import.meta.dirname, 'harness_bridge.py')], {
                windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'], timeout: 115000,
            });
        let output = '';
        child.stdout.setEncoding('utf8').on('data', chunk => {
            output += chunk;
            if (output.length > 128000) child.kill();
        });
        child.stdin.on('error', () => {});
        child.once('error', () => resolve(null));
        child.once('close', code => {
            try { resolve(code === 0 ? JSON.parse(output) : null); }
            catch { resolve(null); }
        });
        child.stdin.end(JSON.stringify({ Event: event, State: state }));
    });
}

export async function sendFixedReply(outbound, endpoint, title, text) {
    // 只有钉钉业务回执 errcode=0 才记为成功；禁止转发临时会话地址。
    const url = new URL(endpoint);
    if (url.origin !== 'https://oapi.dingtalk.com' || url.username || url.password ||
        url.pathname !== '/robot/sendBySession') return false;
    const response = await fetch(url, {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(12000),
        headers: { 'Content-Type': 'application/json', 'x-acs-dingtalk-access-token': await outbound.accessToken() },
        body: JSON.stringify({ msgtype: 'markdown', markdown: { title, text } }),
    });
    return response.ok && (await response.json()).errcode === 0;
}

export async function routeFixed(msg, clientId, send, execute = invoke) {
    if (!isFixed(msg.text)) return false;
    const { senderCorpId: Org, conversationId: Group, senderStaffId: User,
        robotCode: Robot, sourceMsgId: Message, text: Text } = msg;
    // 身份来自 Stream 回调；附件、私聊、其他组织和伪造的 @ 都不得送进业务接口或模型。
    if (msg.conversationType !== 'group' || msg.isInAtList !== true || Robot !== clientId ||
        Org !== process.env.STOCK_OPS_ORG ||
        ![Org, Group, User, Robot, Message].every(value => typeof value === 'string' && value.trim()) ||
        msg.contentParts?.some(part => part.type !== 'text') || msg.imageDownloadCodes?.length) return true;
    const event = { Org, Group, User, Robot, Message, Text };
    const result = await execute(event, 'prepare').catch(() => null);
    // 超时可能已执行，不补发、也不交给 AI 再做一遍。
    if (typeof result?.Text !== 'string' || !result.Text) return true;
    let delivered = false;
    try { delivered = await send(result.Text) === true; }
    catch { /* 投递未知时保留回执，禁止再次发送。 */ }
    finally {
        if (result.Receipt) await execute(event, delivered ? 'sent' : 'unknown').catch(() => {});
    }
    return true;
}

export async function recordStatus(status, modelReady) {
    // 只保存连接状态，不保存会话、凭据或原始错误。
    const file = process.env.STOCK_HARNESS_HEALTH;
    if (!file) return;
    const pending = file + '.' + process.pid + '.pending';
    await writeFile(pending, JSON.stringify({ run: process.env.STOCK_HARNESS_RUN, status, modelReady }));
    await rename(pending, file);
}
