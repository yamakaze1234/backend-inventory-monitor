"""库存管理页面巡检：只读取 DOM，点击展开和翻页，不调用抖店接口。"""
from datetime import datetime
import json
import re
import time
import subprocess
import sys
from pathlib import Path

from douyin_export import ChromeExportWorker, export_lock
from douyin_scope import MACHINE_SCOPE, MACHINE_CATEGORIES, product_url

STOCK_URL = 'https://fxg.jinritemai.com/ffa/morder/stock-manage/list'
PAGE_JS = r'''(() => ({
  shop:document.querySelector('[class*="index_userName"]')?.textContent?.trim(),
  headers:[...document.querySelectorAll('thead th')].map(e=>e.innerText.trim()),
  total:document.querySelector('.auxo-pagination-total-text')?.innerText,
  page:document.querySelector('.auxo-pagination-item-active')?.title,
  next:document.querySelector('li[title="下一页"]')?.getAttribute('aria-disabled'),
  allProducts:[...document.querySelectorAll('[role="tab"][aria-selected="true"]')].some(e=>e.innerText.trim()==='全部商品'),
  category:[...document.querySelectorAll('form .auxo-form-item')].find(e=>e.innerText.startsWith('商品类目'))?.querySelector('.auxo-select-selection-item')?.textContent?.trim(),
  busy:!!document.querySelector('.auxo-spin-spinning'),
  rows:[...document.querySelectorAll('tr[data-row-key]')].map(e=>({
    key:e.getAttribute('data-row-key'),
    cells:[...e.querySelectorAll(':scope > td')].map(c=>c.innerText.trim()),
    expanded:!!e.querySelector('.auxo-table-row-expand-icon-expanded')
  }))
}))()'''


def number(text):
    """空白、负数和加载占位不是零；逗号分组只接受规范格式。"""
    text = str(text).strip()
    return int(text.replace(',', '')) if re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)', text) else None


def inventory_cells(cells):
    if len(cells) != 8:
        raise ValueError('库存表列数变化，本轮未采用。')
    lines = cells[3].splitlines()
    total = number(lines[0]) if lines else None
    spot = re.search(r'^现货\s+([\d,]+)$', cells[3], re.M)
    future = re.search(r'^预售批次\s+([\d,]+)$', cells[3], re.M)
    components = {'总库存': total, '未占用库存': number(cells[4]),
                  '占用库存': 0 if cells[5] == '-' else number(cells[5]),
                  '占用说明': cells[5],
                  '现货可售': number(spot[1]) if spot else (total if len(lines) == 1 else None),
                  '预售库存': number(future[1]) if future else (0 if len(lines) == 1 else None)}
    # 页面只显示预售批次汇总，不虚构阶梯库存的拆分。
    return components


def parse_product(snapshot, product_id):
    """按父子行顺序与完整 ID 归属 SKU；汇总校验拦截展开不完整。"""
    rows = snapshot['rows']
    starts = [i for i, r in enumerate(rows) if r['key'] == 'product-' + product_id]
    if len(starts) != 1:
        raise ValueError('商品行重复或消失，本轮未采用。')
    start = starts[0]
    parent = rows[start]
    cells = parent['cells']
    totals = inventory_cells(cells)
    if not parent.get('expanded') or f'商品ID：{product_id}' not in cells[0]:
        raise ValueError('商品尚未展开或商品 ID 不一致。')
    selling = cells[2].splitlines()[-1]
    product = dict(product_id=product_id, product_name=cells[0].split('\n商品ID：')[0],
                   sale_status=selling, skus=[], product_url=product_url(product_id),
                   category=snapshot.get('category', ''),
                   monitor_scope=MACHINE_SCOPE if snapshot.get('category') in MACHINE_CATEGORIES else '')
    seen = set()
    for row in rows[start + 1:]:
        if row['key'].startswith('product-'):
            break
        match = re.fullmatch(r'sku-([0-9]+)', row['key'])
        if not match or match[1] in seen:
            raise ValueError('SKU 编号缺失或重复，本轮未采用。')
        sid = match[1]
        seen.add(sid)
        c = row['cells']
        parts = inventory_cells(c)
        if not re.search(r'SKU ID:\s*' + re.escape(sid) + r'\s*$', c[0]):
            raise ValueError('SKU 编号与明细不一致。')
        total, available, occupied = (parts[k] for k in ('总库存', '未占用库存', '占用库存'))
        qty = None
        if selling == '已下架' or c[2] == '已下架':
            state = '已下架'
        elif selling != '售卖中' or c[2] != '已上架':
            state = '售卖状态待核对'
        elif any(v is None for v in (total, available, occupied)):
            state = '库存未知'
        elif total != available + occupied:
            state = '库存口径待核对'
        elif occupied:
            state = '活动/渠道库存待核对'
        else:
            qty = available
            state = '在售库存为零' if qty == 0 else ('预售有库存' if parts['现货可售'] == 0 else '在售有库存')
        product['skus'].append(dict(sku_id=sid, sku_name=re.split(r'\nSKU ID:', c[0])[0],
            dom_stock=qty, components=parts, sale_status=selling, sku_status=c[2], stock_state=state,
            source='stock_page'))
    if not product['skus']:
        raise ValueError('未取得 SKU 明细；不会按零库存处理。')
    for field in ('总库存', '未占用库存', '占用库存'):
        values = [s['components'][field] for s in product['skus']]
        if totals[field] is not None and all(v is not None for v in values) and sum(values) != totals[field]:
            raise ValueError('SKU 库存合计与商品汇总不符，请稍后重试。')
    return product


class ChromeStockWorker(ChromeExportWorker):
    """复用现有 Chrome 连接；用实际展开结果确认操作，不信任点击回执。"""
    def snapshot(self, *, require_machine=True):
        self.check_shop()
        for _ in range(30):
            data = self.eval(PAGE_JS)
            if not data['busy']:
                break
            self.pause(.5)
        if data['headers'][:6] != ['商品信息', '库存类型', '商品创建时间', '总库存', '未占用库存', '占用库存']:
            raise ValueError('库存表头变化，本轮未采用。')
        if data['busy']:
            raise ValueError('页面仍在加载，本轮未采用。')
        if not data['allProducts']:
            raise ValueError('库存页面未选择“全部商品”，本轮未采用。')
        if require_machine and data.get('category') not in MACHINE_CATEGORIES:
            raise ValueError('整机类目筛选未生效，本轮未采用；不会混入单品。')
        return data

    def select_machine_category(self):
        """每轮重置后重新选整机类目，查询后再校验完整路径。"""
        self.tap('category')
        for name in ('3C数码及配件', '电脑', 'DIY电脑'):
            self.pause(.3)
            self.tap('category_item', name)
        self.pause(.3)
        names = self.eval("([...document.querySelectorAll('.auxo-select-dropdown [class*=listDataName]')].map(e=>e.innerText.trim()))")
        leaves = [n for n in names if n in ('DIY兼容机', 'DIY整机')]
        if len(leaves) != 1:
            raise ValueError('整机末级类目不唯一或不可用，本轮未采用。')
        self.tap('category_item', leaves[0])
        self.tap('search')
        self.pause(1)

    def tap(self, action, product=None):
        """动作白名单由独立桥接校验，脚本随 EXE 打包。"""
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
        result = subprocess.run([self.command[0], str(root / 'douyin_dom_bridge.mjs')],
            input=json.dumps(dict(session=self.session, page=self.page, shop=self.expected_shop,
                                  action=action, product=product)), capture_output=True,
            text=True, encoding='utf-8', timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not json.loads(result.stdout).get('ok'):
            raise ValueError('页面巡检操作失败；保留历史库存。')

    def read_product(self, pid):
        data = self.snapshot()
        row = next(r for r in data['rows'] if r['key'] == 'product-' + pid)
        if not row['expanded']:
            self.tap('expand', pid)
            self.pause(.5)
        # 个别页面第一次点击只完成定位；重试后必须读到真正展开的明细。
        for attempt in range(3):
            data = self.snapshot()
            try:
                return parse_product(data, pid)
            except ValueError:
                row = next((r for r in data['rows'] if r['key'] == 'product-' + pid), {})
                if row.get('expanded') or attempt == 2:
                    raise
                self.tap('expand', pid)
                self.pause(.5)

    def call(self, op, timeout=3600):
        if op == 'login':
            return super().call(op)
        if op != 'collect':
            raise ValueError('未知页面巡检操作。')
        # 兼容旧页面入口，与官方导出共用同一浏览器连接的互斥锁。
        with export_lock(getattr(self, 'browser_profile', '')):
            if not self.page:
                raise ValueError('请先连接 Chrome。')
            self.open_page(STOCK_URL, 'tr[data-row-key]')
            self.tap('reset')
            self.pause(1)
            self.select_machine_category()
            initial = self.snapshot()
            count_match = re.fullmatch(r'共\s*(\d+)\s*条', initial.get('total') or '')
            if not count_match or initial['page'] != '1':
                raise ValueError('商品总数或第一页未核对，本轮未采用。')
            expected = int(count_match[1])
            products, seen = [], set()
            deadline = time.monotonic() + timeout
            for page in range(1, 1001):
                data = self.snapshot()
                if data['page'] != str(page) or data['total'] != initial['total']:
                    raise ValueError('分页或商品总数发生变化，本轮未采用。')
                ids = [r['key'][8:] for r in data['rows'] if r['key'].startswith('product-')]
                if not ids or any(not p.isdigit() or p in seen for p in ids):
                    raise ValueError('分页重复或为空，本轮未采用。')
                for pid in ids:
                    if time.monotonic() > deadline:
                        raise ValueError('页面巡检超时，历史库存已保留。')
                    products.append(self.read_product(pid))
                    seen.add(pid)
                    if getattr(self, 'progress', None):
                        self.progress(len(products), expected)
                data = self.snapshot()
                if data['total'] != initial['total']:
                    raise ValueError('巡检期间商品总数变化，本轮未采用。')
                if data['next'] == 'true':
                    break
                if data['next'] != 'false':
                    raise ValueError('无法确认下一页状态。')
                self.tap('next')
                for _ in range(20):
                    self.pause(.5)
                    data = self.snapshot()
                    if data['page'] == str(page + 1):
                        break
                else:
                    raise ValueError('翻页未生效，本轮未采用。')
            if len(products) != expected:
                raise ValueError('商品采集数量不完整，本轮未采用。')
            self.check_shop()
            # 仅保存可见商品数据，便于对账，不记录浏览器认证资料。
            folder = self.root / 'page_snapshots' / self.ident
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
            target.write_text(json.dumps(dict(shop=self.expected_shop, complete=True, monitor_scope=MACHINE_SCOPE,
                category=initial['category'], products=products), ensure_ascii=False), encoding='utf-8')
            return dict(ok=True, complete=True, products=products, field='dom_stock', monitor_scope=MACHINE_SCOPE, evidence=str(target))
