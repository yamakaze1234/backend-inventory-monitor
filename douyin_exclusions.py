"""手动下架排除名单：只解析用户输入，不访问商品页面、不修改抖店商品。"""
import re
from urllib.parse import parse_qs, urlsplit


def parse_products(text):
    """保留长编号的文本精度；整批校验通过后才允许保存，避免部分成功。"""
    if not isinstance(text, str):
        raise ValueError('请输入商品 ID 或完整商品链接。')
    tokens = [s for s in re.split(r'[\s,，;；]+', text.strip()) if s]
    if not tokens:
        raise ValueError('请至少填写一个商品 ID 或完整商品链接。')
    if len(tokens) > 500:
        raise ValueError('每次最多添加 500 个链接，请分批添加。')
    products = []
    for token in tokens:
        pid = token
        if not re.fullmatch(r'[0-9]{1,30}', pid):
            try:
                url = urlsplit(token)
                if url.scheme not in ('https', 'http') or url.username or url.password:
                    raise ValueError
                if not any(url.hostname == host or (url.hostname or '').endswith('.' + host)
                           for host in ('jinritemai.com', 'douyin.com')):
                    raise ValueError
                query = parse_qs(url.query)
                identifiers = {v for key in ('id', 'product_id') for v in query.get(key, [])}
                if len(identifiers) != 1:
                    raise ValueError
                pid = identifiers.pop()
                if not re.fullmatch(r'[0-9]{1,30}', pid):
                    raise ValueError
            except ValueError:
                raise ValueError('有一项不是完整商品 ID 或商品详情链接。短链接、科学计数法和分享文案请改为商品 ID；本次尚未保存。') from None
        if pid not in products:
            products.append(pid)
    return products
