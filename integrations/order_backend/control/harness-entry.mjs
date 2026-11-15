import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

// 在同一个 Node 进程内启动 DSH；停止请求走 Harness 自己的 SIGINT 清理流程。
const entry = process.argv[2];
process.argv.splice(1, 2, entry);
const watch = setInterval(async () => {
    const request = await readFile(process.env.STOCK_HARNESS_STOP, 'utf8').catch(() => '');
    if (request !== process.env.STOCK_HARNESS_RUN || !process.listenerCount('SIGINT')) return;
    clearInterval(watch);
    process.emit('SIGINT');
}, 400);
watch.unref();
const { runCli } = await import(pathToFileURL(entry).href);
await runCli();
