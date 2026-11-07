"""整机监控范围与商品链接：按已核实的后台类目识别，不猜商品名称。"""
import re

MACHINE_SCOPE = 'douyin_diy_desktop_v1'
MACHINE_CATEGORIES = ('3C数码及配件/电脑/DIY电脑/DIY兼容机', '3C数码及配件/电脑/DIY电脑/DIY整机')


def product_url(product_id):
    """沿用官方导出表中的商品详情链接格式，只允许完整数字商品 ID。"""
    pid = str(product_id)
    if not re.fullmatch(r'[0-9]{1,30}', pid):
        return ''
    return f'https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id={pid}&origin_type=604'


def machine_rows(rows):
    """历史未核对类目的数据不混入整机列表，必须完成新口径巡检。"""
    return [r for r in rows if r.get('monitor_scope') == MACHINE_SCOPE]
