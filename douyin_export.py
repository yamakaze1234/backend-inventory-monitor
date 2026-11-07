"""Chrome／Edge 官方商品表导出；纯程序执行，不读取认证资料、不调用 AI。"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import os
import threading
import time
import uuid

from douyin_inventory import stock_number
from douyin_scope import MACHINE_SCOPE, MACHINE_CATEGORIES, product_url


PRODUCT_URL = 'https://fxg.jinritemai.com/ffa/g/list'
REPORT_URL = 'https://fxg.jinritemai.com/ffa/g/excel'
EXPORT_FIELD = 'export_sellable'
EXPORT_FIELDS = {EXPORT_FIELD: '现货＋预售＋阶梯库存'}
SHOP_SELECTOR = '[class*="index_userName"]'
# 只读取当前可见导出抽屉的数量，避免隐藏旧抽屉或页面其他文字串入校验。
EXPORT_COUNT_JS = r'''(() => {
  const counts = [...document.querySelectorAll('.ecom-g-drawer-footer-help')]
    .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
    .map(e => e.innerText.match(/当前导出\s*([0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\s*个商品/)?.[1])
    .filter(value => value !== undefined);
  return {count: counts.length === 1 ? counts[0].replaceAll(',', '') : null};
})()'''
_BROWSER_LOCKS = {}
_BROWSER_LOCKS_GUARD = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()


def export_lock(profile):
    """独立登录环境可并行导出；共享默认连接的旧单店模式仍然互斥。"""
    with _BROWSER_LOCKS_GUARD:
        return _BROWSER_LOCKS.setdefault(profile or '__default__', threading.Lock())


def read_product_export(path, expected_products, *, require_category=False):
    """按表头解析官方文件；长 ID、未知数量和错误尺寸均显式处理。"""
    from openpyxl import load_workbook
    workbook = load_workbook(path, read_only=True, data_only=True)
    required = ('商品ID', '商品名称', '规格ID（SKUID）', '商品规格', '现货可售', '预售库存', '阶梯库存')
    try:
        candidates = []
        for sheet in workbook:
            # 官方文件可能将 dimension 写为 A1，不能依赖 max_row/max_column。
            sheet.reset_dimensions()
            rows = sheet.iter_rows(values_only=True)
            header = tuple(str(v).strip() if v is not None else '' for v in next(rows, ()))
            if set(required) <= set(header):
                candidates.append((header, rows))
        if len(candidates) != 1:
            raise ValueError('商品表字段不完整或存在多个数据表，本轮未采用。')
        header, rows = candidates[0]
        if len(set(header)) != len(header):
            raise ValueError('商品表有重复表头，本轮未采用。')
        category_fields = ('一级类目', '二级类目', '三级类目', '四级类目')
        if require_category and not set(category_fields) <= set(header):
            raise ValueError('官方表缺少完整类目字段，无法确认整机范围；保留历史库存。')
        products, seen = {}, set()
        for values in rows:
            if not any(v is not None for v in values):
                continue
            row = dict(zip(header, values))
            ids = [row.get(k) for k in ('商品ID', '规格ID（SKUID）')]
            # 浮点数及 Excel 数字长 ID 可能已经丢精度，拒绝猜测修复。
            if any(not isinstance(v, str) or not v.strip().isascii() or not v.strip().isdigit() for v in ids):
                raise ValueError('商品或 SKU ID 不是完整文本数字，本轮未采用。')
            pid, sid = (v.strip() for v in ids)
            if (pid, sid) in seen:
                raise ValueError('商品表包含重复 SKU，本轮未采用。')
            seen.add((pid, sid))
            components = {k: stock_number(row.get(k)) for k in required[-3:]}
            quantity = None if None in components.values() else sum(components.values())
            # 类目取官方字段，不用商品名称猜整机；四级为空的配件仍计入全表完整性核验。
            levels = [str(row.get(k) or '').strip() for k in category_fields]
            if require_category and not all(levels[:3]):
                raise ValueError('官方表存在未明确类目的商品，本轮未采用。')
            category = '/'.join(levels).rstrip('/')
            product = products.setdefault(pid, dict(product_id=pid, product_name=str(row.get('商品名称') or ''),
                category=category, monitor_scope=MACHINE_SCOPE if category in MACHINE_CATEGORIES else '',
                product_url=product_url(pid), skus=[]))
            if product['category'] != category:
                raise ValueError('同一商品的类目不一致，本轮未采用。')
            product['skus'].append(dict(sku_id=sid, sku_name=str(row.get('商品规格') or ''),
                                        export_sellable=quantity, components=components, source='export',
                                        stock_state='导出库存；上下架未核对'))
        if expected_products <= 0 or len(products) != expected_products:
            raise ValueError(f'导出覆盖不完整：页面 {expected_products} 个商品，表格 {len(products)} 个；保留历史库存。')
        return list(products.values())
    finally:
        workbook.close()


def cli_command():
    """复用用户已装的 OpenCLI 和 Node；不复制 Chrome 资料。"""
    cli = Path.home() / '.opencli/node_modules/@jackwener/opencli/dist/src/main.js'
    node = shutil.which('node')
    if not node:
        candidate = Path('C:/Program Files/nodejs/node.exe')
        node = str(candidate) if candidate.is_file() else None
    if not node or not cli.is_file():
        raise ValueError('缺少 Node 或 OpenCLI，请安装 OpenCLI 并启用Chrome 或 Edge 的 OpenCLI 扩展。')
    return [node, str(cli)]


class ReportPending(ValueError):
    """官方报表尚未就绪，可续取同一任务，不能重新提交。"""


class ChromeExportWorker:
    """串行后台导出；所有点击均使用先查到的唯一可见元素。"""
    def __init__(self, tool_root, profile):
        self.root = Path(profile).resolve().parent.parent
        self.ident = Path(profile).name
        self.session = 'inventory-' + self.ident[:16]
        self.expected_shop = ''
        self.command = cli_command()
        self.stopped = threading.Event()
        self.binding_file = self.root / 'chrome' / (self.ident + '.json')
        try:
            self.page = json.loads(self.binding_file.read_text(encoding='utf-8'))['page']
        except (OSError, ValueError, KeyError):
            self.page = None

    def close(self):
        # 不关闭用户 Chrome；正在执行的短命令结束后会停止，不再发送通知。
        self.stopped.set()

    def report(self, text):
        """长报表任务分阶段回传状态，避免把等待平台误显示成逐链接采集。"""
        callback = getattr(self, 'status', None)
        if callback:
            callback(text)

    def cli(self, *args, timeout=50, raw=False):
        if self.stopped.is_set():
            raise ValueError('导出已停止。')
        try:
            # 显式固定标签 ID，避免用户切换标签或平台打开新页导致误操作其他页。
            command_args = list(map(str, args))
            if self.page and args[0] not in ('bind', 'unbind', 'tab'):
                command_args += ['--tab', self.page]
            result = subprocess.run(self.command + ['browser', self.session, *command_args],
                capture_output=True, text=True, encoding='utf-8', timeout=timeout,
                env=self.browser_environment(),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('浏览器连接超时；请检查浏览器与 OpenCLI 扩展，历史库存已保留。') from None
        output = result.stdout.strip()
        if result.returncode or output.startswith('{"error"'):
            # 仅识别固定故障类型，不展示可能包含地址或认证信息的原始诊断。
            diagnostic = (result.stderr + result.stdout).lower()
            if 'not part of the current browser session' in diagnostic or 'target closed' in diagnostic:
                raise ValueError('浏览器标签连接已失效；请打开正确店铺并点击“连接店铺”，历史库存已保留。')
            if 'not connected' in diagnostic or 'extension' in diagnostic and 'disconnected' in diagnostic:
                raise ValueError('浏览器扩展未连接；请启动所选浏览器 并检查 OpenCLI 扩展。')
            raise ValueError('浏览器操作失败；请检查登录、扩展连接或页面是否变化。')
        if raw:
            return output.split('\n  Update available:')[0].strip()
        try:
            value = json.JSONDecoder().raw_decode(output)[0]
        except ValueError:
            raise ValueError('浏览器未返回有效结果，本轮未采用。') from None
        if isinstance(value, dict) and value.get('error'):
            raise ValueError('页面元素不唯一或不可用，本轮未采用。')
        return value

    def browser_environment(self):
        """扩展连接标识只路由到指定浏览器实例，不读取浏览器登录资料。"""
        env = os.environ.copy()
        profile = getattr(self, 'browser_profile', '')
        if profile:
            env['OPENCLI_PROFILE'] = profile
        return env

    def eval(self, expression):
        return self.cli('eval', expression)

    def pause(self, seconds):
        if self.stopped.wait(seconds):
            raise ValueError('导出已停止。')

    def check_shop(self):
        result = self.eval('({shop:document.querySelector(' + json.dumps(SHOP_SELECTOR) + ')?.textContent?.trim(),url:location.href,browser:navigator.userAgent.includes("Edg/")?"edge":"chrome"})')
        if not result.get('url', '').startswith('https://fxg.jinritemai.com/ffa/') or result.get('shop') != self.expected_shop:
            raise ValueError('浏览器店铺与配置不一致或登录失效；未更新库存，请连接正确店铺。')
        expected = getattr(self, 'expected_browser', '')
        if expected:
            if result.get('browser') != expected:
                raise ValueError('实际连接的浏览器与本店设置不符，请在登录设置中选择对应扩展连接标识')

    def open_page(self, url, ready):
        result = self.cli('open', url)
        self.page = result['page']
        self.cli('wait', 'selector', ready, '--timeout', '20000', raw=True)
        self.check_shop()

    def click(self, selector, text):
        found = self.cli('find', '--css', selector)
        entries = [e for e in found.get('entries', []) if e.get('visible') and e.get('text', '').strip() == text]
        if len(entries) != 1:
            raise ValueError(f'未找到唯一的“{text}”控件；页面可能变化，本轮未采用。')
        # 当前 Chrome 的原生点击会返回成功但页面未响应；沿用已验证的 SDK DOM 点击回退。
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
        result = subprocess.run([self.command[0], str(root / 'douyin_dom_bridge.mjs')],
            input=json.dumps(dict(session=self.session, page=self.page, shop=self.expected_shop,
                                  action='export_click', selector=selector, text=text)),
            capture_output=True, text=True, encoding='utf-8', timeout=30,
            env=self.browser_environment(),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not json.loads(result.stdout).get('ok'):
            raise ValueError(f'“{text}”未完成。')

    def records(self):
        return self.eval('({rows:[...document.querySelectorAll("tr[data-row-key]")].map(e=>({key:e.getAttribute("data-row-key"),time:e.innerText.match(/\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}/)?.[0],ready:[...e.querySelectorAll("a")].some(a=>a.textContent.trim()==="下载报表")}))})')['rows']

    def wait_report_records(self, seconds=10):
        """表格外框先出现，等待实际任务行加载，避免空列表被误判为报表未生成。"""
        deadline = time.monotonic() + seconds
        while True:
            rows = self.records()
            if any(row.get('time') for row in rows) or time.monotonic() >= deadline:
                return rows
            self.pause(.25)

    def refresh_reports(self):
        """同 URL 导航可能复用旧 DOM；切回商品列表再进报表，确保获取新任务状态。"""
        self.open_page(PRODUCT_URL, '#exportSearchedGoods')
        self.open_page(REPORT_URL, 'table')
        self.wait_report_records()

    def request_export(self):
        """提交一个新的官方导出任务，保存不含认证信息的任务标识。"""
        self.open_page(REPORT_URL, 'table')
        old = {r['time'] for r in self.wait_report_records() if r.get('time')}
        self.open_page(PRODUCT_URL, '#exportSearchedGoods')
        self.click('button[type="reset"]', '重置')
        self.click('[role="tab"]', '全部')
        # 等待异步列表稳定，避免读取前一页尚未刷新的总数。
        count, stable = None, 0
        for _ in range(20):
            self.pause(1)
            state = self.eval('({tab:document.querySelector("[role=tab][aria-selected=true]")?.textContent?.trim(),count:document.querySelector(".ecom-g-pagination-total-text")?.innerText.match(/共\\s*(\\d+)\\s*件商品/)?.[1]})')
            current = int(state.get('count') or 0)
            # 页面侧栏存在常驻 spinner；以连续总数、全部标签和导出弹窗数量交叉校验。
            stable = stable + 1 if current == count and current > 0 else 0
            count = current
            if stable >= 2 and state.get('tab') == '全部':
                break
        else:
            raise ValueError('商品总数未稳定或店铺为空，本轮未采用。')
        self.check_shop()
        self.click('#exportSearchedGoods', '导出查询商品')
        self.wait_export_count(count)
        started = time.time()
        self.click('button.ecom-g-btn-primary', '导出')
        self.pause(4)
        return {"started": started, "count": count, "old": sorted(old), "shop": self.expected_shop}

    def wait_export_count(self, expected, seconds=10):
        """弹窗标题先于数量渲染，等待完整数值，不把加载中的空白当作零。"""
        if expected >= 500000:
            raise ValueError(f'查询共 {expected} 个商品，达到平台导出上限，本轮未采用。')
        deadline, observed = time.monotonic() + seconds, None
        while time.monotonic() < deadline:
            raw = self.eval(EXPORT_COUNT_JS).get('count')
            observed = int(raw) if isinstance(raw, str) and raw.isascii() and raw.isdigit() else None
            if observed == expected:
                return observed
            self.pause(.2)
        if observed in (None, 0):
            raise ValueError(f'导出窗口数量尚未加载完成（查询 {expected} 个商品），本轮未采用，请稍后重试。')
        raise ValueError(f'导出数量与查询不一致（查询 {expected} 个，导出 {observed} 个），本轮未采用。')

    def call(self, op, timeout=300):
        with export_lock(getattr(self, 'browser_profile', '')):
            if op == 'login':
                # 只在用户主动连接时绑定当前普通 Chrome；定时任务绝不随意重新绑定。
                self.page = None
                self.cli('bind')
                self.open_page(PRODUCT_URL, '#exportSearchedGoods')
                self.binding_file.parent.mkdir(parents=True, exist_ok=True)
                self.binding_file.write_text(json.dumps({'page': self.page}), encoding='utf-8')
                return {'ok': True}
            if op != 'collect':
                raise ValueError('未知导出操作。')
            if not self.page:
                raise ValueError('请先连接所选浏览器，确认店铺后再导出。')
            pending_file = self.root / 'chrome' / (self.ident + '.pending.json')
            try:
                pending = json.loads(pending_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pending = {}
            if pending.get('shop') == self.expected_shop and pending.get('started') and time.time() - pending['started'] > 3600:
                raise ValueError('同一报表已等待超过一小时，请核对平台导出记录；未重复提交报表。')
            if pending.get('shop') != self.expected_shop or not pending.get('started'):
                self.report('正在提交官方商品表导出；保留上次有效库存')
                pending = self.request_export()
                pending_file.parent.mkdir(parents=True, exist_ok=True)
                pending_file.write_text(json.dumps(pending, ensure_ascii=False), encoding='utf-8')
            started, count, old = pending['started'], pending['count'], set(pending['old'])
            deadline = time.monotonic() + min(timeout, 600)
            # 按本次新生成时间匹配报表，不点击历史第一条，也不复用旧下载。
            record = None
            self.report('正在等待官方报表生成；保留上次有效库存')
            for attempt in range(20):
                if time.monotonic() >= deadline:
                    break
                self.refresh_reports()
                fresh = [r for r in self.records() if r.get('time') and r['time'] not in old
                         and started - 5 <= datetime.strptime(r['time'], '%Y-%m-%d %H:%M:%S').timestamp() <= started + 60]
                if len(fresh) > 1:
                    raise ValueError('同时出现多份新报表，无法确定归属，本轮未采用。')
                if fresh and fresh[0]['ready']:
                    record = fresh[0]
                    break
                # 慢报表逐步拉长查询间隔，不每五秒反复刷新；到期保留同一个导出任务。
                self.pause(min(30, 5 * (attempt + 1), max(0, deadline - time.monotonic())))
            if not record:
                raise ReportPending('报表仍在生成；30 秒后自动续取同一任务。')
            # 报表生成并行，下载认领短暂串行，防止相同文件名前缀被不同店铺串读。
            with _DOWNLOAD_LOCK:
                return self.download_report(record, started, count, pending_file)

    def download_report(self, record, started, count, pending_file):
        """持有下载锁时才点击、认领及归档本店新文件。"""
        self.check_shop()
        self.report('正在下载并校验官方商品表；保留上次有效库存')
        clicked = time.time()
        key = record['key']
        if not str(key).isdigit():
            raise ValueError('报表行标识变化，本轮未采用。')
        self.click(f'tr[data-row-key="{key}"] a', '下载报表')
        # 输出中可能包含临时下载地址，仅在内存取文件路径，不记录地址。
        download = self.cli('wait', 'download', 'productExport-', '--timeout', '30000', timeout=40)
        source = Path(download.get('filename') or '')
        if (not download.get('downloaded') or download.get('state') != 'complete'
                or not source.is_file() or source.suffix.lower() != '.xlsx'
                or source.stat().st_mtime < clicked - 2):
            raise ValueError('未取得本次完整下载文件，本轮未采用。')
        self.check_shop()
        machine_only = getattr(self, 'machine_only', False)
        products = read_product_export(source, count, require_category=machine_only)
        all_skus = sum(len(p['skus']) for p in products)
        # 先核验全表覆盖，再从官方四级类目筛整机；不用历史页面名单漏掉新商品。
        if machine_only:
            products = [p for p in products if p['monitor_scope'] == MACHINE_SCOPE]
        folder = self.root / 'exports' / self.ident
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (datetime.fromtimestamp(started).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6] + '.xlsx')
        shutil.copy2(source, target)
        evidence = {'shop': self.expected_shop, 'scope': '商品管理/全部/重置筛选', 'product_count': count,
                    'sku_count': all_skus, 'monitor_product_count': len(products),
                    'monitor_sku_count': sum(len(p['skus']) for p in products), 'report_time': record['time'],
                    'file': str(target), 'download_file': str(source.resolve()),
                    'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
        target.with_suffix('.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        pending_file.write_text('{}', encoding='utf-8')
        return {'ok': True, 'complete': True, 'products': products, 'export': evidence,
                'field': EXPORT_FIELD, 'monitor_scope': MACHINE_SCOPE if machine_only else ''}


class ChromeMachineExportWorker(ChromeExportWorker):
    """整机主采集入口：一次官方导出，在本地筛选类目，不逐个展开 SKU。"""
    machine_only = True
