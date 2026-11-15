"""自动巡检终态告警：独立订阅、每轮每接收方至多尝试一次。"""
from datetime import datetime

CHANNEL = 'douyin_patrol_failure'


def payload(shop, started, finished, reason, *, snapshot_saved=False):
    """只接收运行时已脱敏的原因，区分采集失败与采集后的同步失败。"""
    if snapshot_saved:
        impact = '本轮库存已采集并保存，但后续同步或通知未完成；在线表和群消息可能尚未更新。'
        advice = '请查看本轮运行说明和通知记录，核对在线表授权及网络连接后重新同步。'
    else:
        impact = '本轮库存结果未确认，页面保留上次采集数据，不能作为当前库存依据。'
        advice = '在软件中打开该店铺浏览器，确认登录正常、页面加载完成后，点击「校验本店连接」并重新巡检。'
    text = (f'【自动巡检失败】\n\n店铺：{shop}\n功能：前台库存监控\n'
            f'时间：{datetime.fromtimestamp(finished):%Y-%m-%d %H:%M:%S}\n'
            f'耗时：{max(0, round(finished - started))} 秒\n\n'
            f'失败原因：{reason}\n影响说明：{impact}\n处理建议：{advice}')
    return {'msgtype': 'text', 'text': {'content': text}}


def deliver(robots, run_id, shop, started, finished, reason, *, snapshot_saved=False):
    """持久回执覆盖重入；投递未知或失败不自动补发，避免刷群。"""
    return robots.send(CHANNEL, run_id,
                       payload(shop, started, finished, reason, snapshot_saved=snapshot_saved),
                       started, retry_failed=False)
