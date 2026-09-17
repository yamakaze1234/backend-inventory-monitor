"""Render the maintained Chinese guide into a self-contained offline HTML file."""
import base64
import html
from pathlib import Path
import re
import sys


def inline(text):
    text = html.escape(text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<a href="\2" target="_blank" rel="noopener">\1 ↗</a>', text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    return re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)


def build(destination, screenshot=None):
    lines=Path('inventory-setup.md').read_text(encoding='utf-8').splitlines()
    content=[];toc=[];i=0;n=0
    while i<len(lines):
        line=lines[i].strip()
        if not line or line.startswith('# '): i+=1;continue
        if line.startswith('## '):
            n+=1;title=line[3:];toc.append(f'<a href="#s{n}">{html.escape(title)}</a>')
            content.append(f'<h2 id="s{n}">{inline(title)}</h2>');i+=1;continue
        if line.startswith('### '):content.append('<h3>'+inline(line[4:])+'</h3>');i+=1;continue
        if line.startswith('```'):
            block=[];i+=1
            while i<len(lines) and not lines[i].startswith('```'):block.append(lines[i]);i+=1
            content.append('<pre><code>'+html.escape('\n'.join(block))+'</code></pre>');i+=1;continue
        if line.startswith('|'):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                cells=[c.strip() for c in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r'[-: ]+',c) for c in cells):rows.append(cells)
                i+=1
            table='<div class="table"><table><thead><tr>'+''.join('<th>'+inline(c)+'</th>' for c in rows[0])+'</tr></thead><tbody>'
            table+=''.join('<tr>'+''.join('<td>'+inline(c)+'</td>' for c in row)+'</tr>' for row in rows[1:])
            content.append(table+'</tbody></table></div>');continue
        if re.match(r'^(\d+\. |- )',line):
            ordered=bool(re.match(r'^\d+\.',line));tag='ol' if ordered else 'ul';items=[]
            while i<len(lines) and re.match(r'^\d+\. ' if ordered else r'^- ',lines[i].strip()):
                items.append('<li>'+inline(re.sub(r'^(\d+\. |- )','',lines[i].strip()))+'</li>');i+=1
            content.append('<'+tag+'>'+''.join(items)+'</'+tag+'>');continue
        content.append('<p>'+inline(line)+'</p>');i+=1
    picture=''
    if screenshot and Path(screenshot).is_file():
        data=base64.b64encode(Path(screenshot).read_bytes()).decode()
        picture=f'<figure><img alt="库存监控页：自动重登开关位置，示例数据" src="data:image/png;base64,{data}"><figcaption>库存页顶部：自动重登开关。图为离线验收界面，商品与机器人信息为示例。</figcaption></figure>'
    css='''*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:26px}body{margin:0;background:#f1f5fb;color:#223550;font:16px/1.85 "Microsoft YaHei",system-ui,sans-serif}aside{position:fixed;inset:0 auto 0 0;width:264px;background:#101c30;color:#d5e1f5;padding:28px 20px;overflow:auto}aside b{color:white;font-size:20px}aside small{display:block;color:#93a9c8;margin:8px 0 20px}nav a{display:block;text-decoration:none;color:#c2d3eb;padding:7px 10px;margin:3px 0;border-radius:6px;font-size:13px;line-height:1.6}nav a:hover{background:#223550;color:white}main{margin-left:264px;max-width:1400px;padding:40px 5vw 90px}.hero{background:#fff;padding:32px;border:1px solid #c3d3eb;border-radius:12px;margin-bottom:30px}h1{font-size:34px;line-height:1.35;margin:10px 0;color:#173453}.eyebrow{color:#285eb9;font-size:13px;font-weight:700;letter-spacing:1px}.lead{color:#586d8c;margin:12px 0}.pills{display:flex;gap:8px;flex-wrap:wrap}.pills span{background:#edf3fc;color:#285eb9;padding:3px 12px;border-radius:20px;font-size:13px}h2{margin:44px 0 18px;font-size:24px;line-height:1.5;border-bottom:2px solid #c3d3eb;padding-bottom:12px}h3{font-size:19px;margin-top:26px}p{margin:12px 0}a{color:#285eb9}li{margin:9px 0}li::marker{color:#285eb9;font-weight:700}strong{color:#173453}code{background:#e5edf7;border-radius:4px;padding:2px 5px;font:14px/1.7 Consolas,monospace;overflow-wrap:anywhere}pre{padding:20px;background:#142238;color:#e0eaff;overflow:auto;border-radius:8px}pre code{background:none;color:inherit;padding:0;white-space:pre}table{border-collapse:collapse;width:100%;background:white;font-size:14px}th,td{text-align:left;vertical-align:top;padding:13px 15px;border:1px solid #c3d3eb}th{background:#e8eff9;color:#173453}tr:nth-child(even){background:#f8faff}.table{overflow:auto;margin:20px 0}img{max-width:100%;height:auto;border:1px solid #c3d3eb;border-radius:8px}figure{margin:24px 0}figcaption{font-size:13px;color:#586d8c}button{background:#285eb9;border:0;border-radius:6px;color:white;padding:9px 16px;cursor:pointer;font:inherit}.tools{margin-top:20px;display:flex;gap:12px;align-items:center;font-size:13px}.tools a{text-decoration:none}@media(max-width:950px){aside{position:static;width:auto;max-height:290px}main{margin:0;padding:24px 18px}h1{font-size:27px}nav{columns:2}.hero{padding:24px}}@media print{aside,.tools{display:none}main{margin:0;padding:0;max-width:none}body{background:white;font-size:11pt}h2{break-after:avoid}tr,figure{break-inside:avoid}.hero{border:0;padding:0}a{color:inherit;text-decoration:none}pre{white-space:pre-wrap}.table{overflow:visible}}'''
    page='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>后台库存监控 v1.03 · 完整使用教程</title><style>'+css+'</style></head><body><aside><b>后台库存监控</b><small>完整使用教程 · v1.03<br>采集脚本 0.4.0</small><nav>'+''.join(toc)+'</nav></aside><main><header class="hero"><div class="eyebrow">安装 · 配置 · 使用 · 故障处理</div><h1>连接 SQL 库存<br>按仓库监控变化</h1><p class="lead">默认直接查询 SQL，按需切换脚本采集、配置机器人及钉钉监听。本文明确区分实测结果、环境要求与尚未验证的边界。</p><div class="pills"><span>Windows 11 x64 已实测</span><span>Win7 不支持</span><span>本机执行，无需云端 AI</span></div><div class="tools"><button onclick="window.print()">打印 / 保存 PDF</button><span>双击即可离线阅读 · Ctrl+F 查找 · 更新于 2026-09-17</span></div></header>'+picture+''.join(content)+'</main></body></html>'
    Path(destination).write_text(page,encoding='utf-8')
    return page


if __name__=='__main__':
    build(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else None)
