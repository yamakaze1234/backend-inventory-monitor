"""把各功能已有的计划放到同一页，保存仍走各自的业务入口。"""
from datetime import datetime
import sqlite3

from inventory_business_views import A, F, S
from price_schedule import TZ


def next_line(timestamp):
    if timestamp:
        return '下次：'+datetime.fromtimestamp(timestamp,TZ).strftime('%m-%d %H:%M')
    return ''


def plan(key,title,description,summary,state='',tone='neutral',action=None,**extra):
    card=S(key,title,description=description,summary=summary,status=state,tone=tone,**extra)
    if action:
        card['forms'].append(action)
    return card


def view(b,q):
    from inventory_cache_schedule import schedule_form as cache_form, settings as cache_settings
    from inventory_daily import settings as daily_settings
    from order_business import adapter, patrol_window_form

    # 只读库存计划键，避免为查看设置重新加载整个库存列表。
    with b.service.db() as db:
        interval=b.service.get(db,'interval_seconds',7200)//60
        enabled=b.service.get(db,'enabled',True)
        next_inventory=b.service.get(db,'next_check',0) if enabled else 0
    inventory=plan('inventory','配件库存监控','定时检查已关注配件的库存。',f'每 {interval} 分钟',
                   '已启用' if enabled else '已暂停','active' if enabled else 'neutral',
                   A('interval','设置库存监控周期',[F('minutes','监控周期（分钟）','number')],
                     dict(minutes=interval),module='settings',submitLabel='保存周期'),note=next_line(next_inventory))

    # 订单使用独立队列；暂时不可读时仍能查看和修改其他计划。
    order_error=False
    try:
        order_state=adapter(b).configuration()
    except (OSError,ValueError,sqlite3.Error):
        order_error=True
        order_state={}
    order_form=patrol_window_form(order_state)
    if order_form:
        schedule=order_form['values']
        start,end=schedule['start'],schedule['end']
        window=order_state.get('patrolWindow') or {}
        end_label=('次日 ' if end<start else '')+end
        summary=f"{start}–{end_label} · 每 {schedule['intervalMinutes']} 分钟"
        valid=window.get('valid',True)
        paused=order_state.get('schedulePaused',True)
        enabled=order_state.get('serviceEnabled',False)
        order=plan('orders','订单自动巡检','时段只限制自动启动；手动匹配和手动巡检随时可用。',summary if valid else '请重新保存有效的时段',
                   '时段待修正' if not valid else '已暂停' if paused else '后台已停用' if not enabled else '已启用',
                   'warning' if not valid else 'active' if enabled and not paused else 'neutral',order_form)
    elif order_error:
        order=plan('orders','订单自动巡检','请刷新页面或到订单机器人设置检查连接。','暂时无法读取','读取失败','warning')
    elif order_state.get('configured'):
        order=plan('orders','订单自动巡检','到订单机器人设置安装支持巡检时段的兼容后端。','需要兼容后端')
    else:
        order=plan('orders','订单自动巡检','先到订单机器人设置完成连接和后端配置。','尚未配置')

    price=b.profit().schedule_settings()
    minutes=price['intervalSeconds']//60
    daily=price['scheduleMode']=='daily'
    price_plan=plan('price','ERP 调价监控','按周期或每日固定时间检测维护表配件的 ERP 单价。',
                    '每天 '+'、'.join(price['scheduleTimes']) if daily else f'每 {minutes} 分钟',
                    '已启用' if price['enabled'] else '已暂停' if price['bound'] else '待绑定维护表',
                    'active' if price['enabled'] else 'neutral',
                    A('schedule','设置调价检测计划',[F('enabled','启用定时检测','bool'),
                      F('mode','运行方式',options=[('daily','每天固定时间'),('interval','按分钟间隔')]),
                      F('times','北京时间','times',limit=24,when=dict(mode='daily')),
                      F('minutes','检测间隔（分钟）','number',when=dict(mode='interval'))],
                      dict(enabled=price['enabled'],mode='daily' if daily else 'interval',minutes=minutes,
                           times=price['scheduleTimes']),module='profit',submitLabel='保存计划'),
                    note=next_line(price['nextCheck']) if price['enabled'] else '')

    report=daily_settings(b.service)
    report_plan=plan('daily','货源日报','按北京时间生成并发送货源日报。','每天 '+'、'.join(report['times']),
                     '已启用' if report['enabled'] else '已停用','active' if report['enabled'] else 'neutral',
                     A('settings','设置日报时间',[F('enabled','启用定时日报','bool'),F('times','北京时间','times',limit=12)],
                       dict(enabled=report['enabled'],times=report['times']),module='daily',submitLabel='保存计划'))

    stores=b.douyin.store.stores() if b.douyin else []
    shop_rows=[]
    for shop in stores:
        minutes=shop['interval']//60
        selected=shop.get('patrol_selected',True)
        shop_rows.append(dict(id=shop['id'],name=shop['name'],summary=f'每 {minutes} 分钟',
                              status='未参与巡检' if not selected else '已启用' if shop['enabled'] else '已暂停',
                              tone='active' if selected and shop['enabled'] else 'neutral',
                              action=A('interval','设置巡检周期',[F('minutes','巡检周期（分钟）','number')],
                                       dict(shop=shop['id'],minutes=minutes),module='douyin',
                                       description='仅修改「'+shop['name']+'」的周期。',submitLabel='保存周期')))
    shops=plan('shops','抖店库存巡检','每个店铺独立设定周期。参与巡检与暂停开关仍在前台库存监控。',
               f'{len(shop_rows)} 家店铺' if shop_rows else '尚未添加店铺',shopRows=shop_rows)

    cache=cache_settings(b.service)
    cleanup=plan('cache','定期缓存清理','程序空闲时按计划清理，手动预览和清理仍在本地缓存。',
                 f"每 {cache['period_days']} 天 · {cache['time']}",
                 '已启用' if cache['enabled'] else '已停用','active' if cache['enabled'] else 'neutral',
                 cache_form(cache),note=next_line(cache['state'].get('next_run')) if cache['enabled'] else '')
    return dict(module='schedules',filters=[],query=q,sections=[inventory,order,shops,price_plan,report_plan,cleanup])
