"""重构候选版的离线验收与体验入口；独立数据目录，所有外部发送均禁用。"""
from contextlib import ExitStack
from datetime import datetime, timezone
import copy
import gc
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from inventory_sources import InventoryRuntimeService
from inventory_workspace import build_workspace
from inventory_impact import InventoryImpact
from inventory_delivery import format_events
from profit_manifest import sha


class DemoSource:
    """仅提供内存样例，绝不连接真实 SQL。"""
    scope = 'sql:offline-refactor-fixture'
    legacy_text = False

    def __init__(self):
        names = ['GIGABYTE技嘉 GV-N506TEAGLEOC ICE-16GD（RTX5060Ti冰猎鹰 16G）',
                 'GIGABYTE技嘉 GV-N506TEAGLE OC-16GD（RTX5060Ti猎鹰 16G）',
                 'GIGABYTE技嘉 GV-N506TGAMING OC-16GD（RTX5060Ti魔鹰 16G）',
                 'XPG 威龙 D300G 24G 6000 C40 RGB', 'Intel Ultra 7 270K Plus', '技嘉 B860M 魔鹰 X']
        self.rows = [dict(goods_id=str(94015+i), 商品编码=f'SAMPLE-{i}', 商品名称=name,
                          库房='公司大库', 分库数=8, 分库可销数=8, 分库待入=0) for i,name in enumerate(names)]
        self.catalog = [dict(goods_id=r['goods_id'], 商品编码=r['商品编码'], 商品名称=r['商品名称'], 总数量=8, 可销数=8, 待入=0) for r in self.rows]

    def read_bundle(self, goods_ids=None):
        return copy.deepcopy(dict(catalog=self.catalog, warehouses=self.rows))


def seed(folder):
    service = InventoryRuntimeService(folder)
    service.offline_preview = True
    source = DemoSource()
    worker = service.sql_worker
    worker.configure(source)
    worker.step()
    for row in source.rows:
        service.save_product(dict(sku=row['商品编码'], goods_id=row['goods_id'], name=row['商品名称'], threshold=5))
    worker.last_call = 0
    service.request_check()
    worker.step()
    for i, (stock, able) in enumerate([(335,335),(125,124),(9,8),(0,0),(8,8),(8,8)]):
        source.rows[i].update(分库数=stock, 分库可销数=able)
    source.rows[4]['商品编码'] = source.catalog[4]['商品编码'] = 'CHANGED-CODE'
    source.rows.pop()
    worker.last_call = 0
    service.request_check()
    worker.step()
    configs = [dict(shopId='英特尔旗舰店', configId=f'config-{i}', product=f'乔思伯整机 / 链接 {26+i}',
                    name=f'配置{i+1}', priceCents=599900, feeCents=0, installment=0,
                    parts=[dict(lineId='memory', slot='内存', name=source.rows[3]['商品名称'], goodsId='94018', qty=1, taxCents=29900)]) for i in range(4)]
    manifest = dict(contract='profit-monitor/v1', schemaVersion=1, complete=True, formulaVersion='profit/v1',
                    sourceId='offline-demo', workspaceId='offline-workspace', revision=1,
                    publishedAt=datetime.now(timezone.utc).isoformat(), configs=configs, businessHash=sha(configs))
    manifest['contentHash'] = sha(manifest)
    path = folder / '示例配置快照.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    model = InventoryImpact(service)
    model.import_manifest(path)
    from douyin_inventory import DouyinInventory
    from douyin_scope import MACHINE_SCOPE
    shops = DouyinInventory(folder)
    store = shops.add_store('英特尔旗舰店（模拟）')
    shops.rebase(store, confirmed=True)
    products = [dict(product_id=str(90000+i), product_name=c['product'], monitor_scope=MACHINE_SCOPE,
                     skus=[dict(sku_id=str(91000+i), sku_name=c['name'], export_sellable=10 if i != 1 else 0,
                                components={'现货可售': 0, '预售库存': 10 if i != 1 else 0, '阶梯库存': 0},
                                erp=dict(source='maintenance',state='配件齐全',unknown=[],shortages=[],
                                         parts=[dict(goods_id='101',required=1,available=10)],checked=1800000000))]) for i,c in enumerate(configs)]
    shops.accept(store, shops.store(store)['revision'], products, complete=True, notify=False)
    # 多店页面同时展示已配置和待配置状态，后两店不接入真实浏览器。
    shops.add_store('技嘉旗舰店')
    shops.add_store('乔思伯官方旗舰店')
    shops.enable(store, True)
    shops.accept(store, shops.store(store)['revision'], products, complete=True, notify=False)
    for i in range(3):
        model.bind_listing('英特尔旗舰店', f'config-{i}', spu=str(26+i), store_id=store, product_id=str(90000+i), sku_id=str(91000+i))
    # 所有库存通知仅作为本地预览记录；不进入真实投递队列。
    with service.db() as db:
        db.execute("UPDATE events SET status='preview'")
    return service


def run(output_directory=None, *, interactive=False):
    from PIL import ImageGrab
    from inventory_insight_ui import show_coverage
    from inventory_version import APP_TITLE
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    output = Path(output_directory).resolve() if output_directory else Path(tempfile.gettempdir()) / 'inventory-refactor-demo'
    output.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix='offline-data-', dir=output))
    errors = []
    def blocked(*args, **kwargs):
        raise RuntimeError('离线验收模式：外部采集、子进程和消息发送已禁用。')
    # 防止页面中任何意外操作访问外部服务；真实通知路径不参与验收。
    with ExitStack() as stack:
        for target in ('notification_robots.send_encrypted', 'urllib.request.urlopen', 'socket.socket.connect',
                       'subprocess.Popen', 'sql_inventory_source.SqlSource._read_queries',
                       'douyin_inventory_runtime.DouyinRuntime.worker'):
            stack.enter_context(patch(target, side_effect=blocked))
        service = seed(data)
        # 目录读取不得阻塞首页；搜索页的同版数据必须复用，避免每次按键重复读全库。
        from unittest.mock import Mock
        service.catalog = Mock(wraps=service.catalog)
        # 离线验收用 update 驱动 Tk，未进入 mainloop；集中在主线程回收窗口变量，
        # 避免后台线程触发循环 GC 后等待不存在的 Tk 主循环。
        gc_was_enabled = gc.isenabled()
        gc.disable()
        root = tk.Tk()
        root.title(APP_TITLE + ' · 离线模拟数据 / 不发送消息')
        root.geometry('1380x900+30+30')
        root.minsize(1080, 680)
        root.option_add('*Font', ('Microsoft YaHei UI', 10))
        root.report_callback_exception = lambda kind, value, trace: errors.append(str(value))
        # 全部导航入口都在隔离数据下验收，钉钉状态使用本地替身。
        controller = SimpleNamespace(root=data, status=lambda: dict(state='paused', ready=0, sources=[]))
        panel = build_workspace(root, service, live=False, dingtalk_controller=controller)
        try:
            assert service.catalog.call_count == 0, '启动首页不应读取隐藏商品目录'
            root.update()
            # 验证静态轮询不会重写列表，记录 UI 线程用时，避免重绘回归。
            from unittest.mock import patch as mock_patch
            timings = []
            panel.refresh(); root.update()
            with mock_patch('inventory_panel.sync_tree', wraps=__import__('inventory_theme').sync_tree) as sync:
                for _ in range(20):
                    began = time.perf_counter()
                    panel.refresh(); root.update_idletasks()
                    timings.append(round((time.perf_counter() - began) * 1000, 2))
                assert sync.call_count == 0, '不变的库存状态仍在刷新表格'
            (output / 'refresh-timing.json').write_text(json.dumps(timings), encoding='utf-8')
            if interactive:
                root.mainloop()
            else:
                for width,height in [(1380,900),(1080,680)]:
                    root.geometry(f'{width}x{height}+30+30')
                    for module in ('monitor','dingtalk','search','config','settings','robots','daily','profit','events','douyin','impact'):
                        panel.open_settings(module) if module == 'robots' else panel.show_module(module)
                        root.update()
                        selected_key = 'settings' if module == 'robots' else 'monitor' if module in ('search', 'config') else module
                        assert panel.navigation_buttons[selected_key].cget('bg') == '#202D44'
                        check_button = panel.navigation_buttons['check']
                        assert check_button.winfo_rooty() < panel.sidebar_erp_status.winfo_rooty()
                        assert check_button.winfo_rooty() > max(b.winfo_rooty() for key,b in panel.navigation_buttons.items() if key not in ('check', 'settings'))
                        assert panel.navigation_buttons['settings'].winfo_rooty() > check_button.winfo_rooty()
                        assert 'search' not in panel.navigation_buttons
                        if module == 'dingtalk':
                            panel.dingtalk_page.content.winfo_children()[0].refresh()
                        # 必须在隐藏库存提示控件后再次刷新，才能复现原来的错误状态误报。
                        for _ in range(2):
                            panel.refresh()
                            assert not panel._refresh_error, (module, panel._refresh_error)
                            assert '失败' not in panel.sidebar_erp_status.cget('text'), module
                        if module == 'impact':
                            tree = panel.impact_page.tree
                            assert len(tree.get_children()) == 4
                            tree.selection_set(tree.get_children()[0])
                        elif module == 'events':
                            tree = panel.event_page.tree
                            assert len(tree.get_children()) >= 4
                            tree.selection_set(tree.get_children()[0])
                        elif module == 'douyin':
                            page = panel.douyin_page
                            assert page.ident, '已有店铺应自动选中'
                            # 勾选与查看独立：取消另一店后切换结果，巡检范围仍保持原选择。
                            assert page.export_button.cget('text') == '立即检查'
                            assert len(page.patrol_widgets) == 3
                            other_id = next(key for key in page.patrol_widgets if key != page.ident)
                            item = page.patrol_widgets[other_id]
                            item['check'].invoke(); root.update()
                            assert not page.runtime.store.store(other_id)['patrol_selected']
                            previous = page.shop.current()
                            page.shop.current(next(i for i,r in enumerate(page.shop_rows) if r['id'] == other_id))
                            page.select_store(); root.update()
                            assert not item['value'].get()
                            page.shop.current(previous); page.select_store()
                            item['check'].invoke(); root.update()
                            for patrol in page.patrol_widgets.values():
                                assert patrol['check'].winfo_ismapped()
                                frame = patrol['frame']
                                assert frame.winfo_x() + frame.winfo_width() <= page.patrol_box.winfo_width()
                            ImageGrab.grab(window=int(root.frame(),16)).save(output / f'douyin-patrol-{width}.png')
                            assert page.stock.winfo_height() >= 100, '最小窗口也必须显示库存行'
                            assert len(page.stock.get_children()) == 4, '已有基线应立即显示'
                            # 检查真实层级和相邻库存列，不只确认行数。
                            first = page.stock.get_children()[0]
                            children = page.stock.get_children(first)
                            assert len(children) == 1 and page.stock.item(children[0], 'text')
                            assert page.stock.set(children[0], 'quantity') == '10'
                            page.stock.selection_set(children[0])
                            assert 'id=90000' in page.selected_product_url()
                            page.filter.set('库存为零')
                            assert len(page.stock.get_children()) == 1
                            page.filter.set('全部库存')
                            # 真正走入库后的汇总预览，核验文案与复制入口，不调用机器人服务。
                            page.tabs.select(1); root.update()
                            assert '1 个链接 · 1 个零库存 SKU' in page.restock.text
                            assert 'SKU ID：91001' not in page.restock.text
                            assert len(page.restock.text) < 500
                            assert '售罄' not in page.restock.text
                            assert str(page.restock.copy.cget('state')) == 'normal'
                            ImageGrab.grab(window=int(root.frame(),16)).save(output / f'restock-preview-{width}.png')
                            (output / '补库存机器人文案样例.md').write_text(page.restock.text,encoding='utf-8')
                            page.tabs.select(0); root.update()
                            # 展开设置后检查窄窗口内的操作仍然可见。
                            page.toggle_settings(); root.update()
                            ImageGrab.grab(window=int(root.frame(),16)).save(output / f'douyin-settings-{width}.png')
                            for line in page.settings_box.winfo_children():
                                for control in line.winfo_children():
                                    assert control.winfo_ismapped() and control.winfo_width() > 5, '设置按钮不可见'
                                    assert control.winfo_x() + control.winfo_width() <= line.winfo_width(), '设置按钮超出窗口'
                            page.toggle_settings()
                            if width == 1380:
                                manager = page.shops_dialog(); root.update()
                                assert len(manager.tree.get_children()) == 3
                                for control in manager.actions.winfo_children():
                                    assert control.winfo_ismapped() and control.winfo_height() > 5
                                    assert control.winfo_rootx() + control.winfo_width() <= manager.winfo_rootx() + manager.winfo_width()
                                ImageGrab.grab(window=int(manager.frame(),16)).save(output / 'shop-manager.png')
                                manager.close()
                                # 用真实窗口提交人工排除，再移出名单；测试只读模拟库存，不访问线上表。
                                popup = page.exclusions_dialog(); root.update()
                                popup.input.insert('1.0','90001'); popup.add()
                                deadline = time.monotonic() + 6
                                while popup.pending and time.monotonic() < deadline:
                                    root.update(); time.sleep(.05)
                                assert popup.pending is None and len(popup.tree.get_children()) == 1
                                assert page.runtime.store.restock_preview(page.ident)['sku_count'] == 0
                                ImageGrab.grab(window=int(popup.frame(),16)).save(output / 'excluded-links.png')
                                popup.tree.selection_set('90001'); popup.remove()
                                deadline = time.monotonic() + 6
                                while popup.pending and time.monotonic() < deadline:
                                    root.update(); time.sleep(.05)
                                assert popup.pending is None and not popup.tree.get_children()
                                assert page.runtime.store.restock_preview(page.ident)['sku_count'] == 1
                                popup.close()
                                # 新设置窗口必须显示文件选择和两个浏览器选项，离线不打开浏览器或文件。
                                from douyin_low_stock_ui import open_rules
                                popup = open_rules(page); root.update()
                                ImageGrab.grab(window=int(popup.frame(),16)).save(output / 'low-stock-settings.png')
                                popup.destroy()
                                popup = page.maintenance_settings(); root.update()
                                ImageGrab.grab(window=int(popup.frame(),16)).save(output / 'maintenance-settings.png')
                                popup.destroy()
                                popup = page.login_settings(); root.update()
                                assert popup.native_frame.winfo_ismapped()
                                popup.connection_mode.set('现有浏览器扩展（兼容）'); root.update()
                                assert popup.extension_frame.winfo_ismapped() and not popup.native_frame.winfo_ismapped()
                                popup.connection_mode.set('独立店铺浏览器（推荐）'); root.update()
                                for control in popup.login_controls:
                                    assert control.winfo_ismapped() and control.winfo_height() > 5
                                ImageGrab.grab(window=int(popup.frame(),16)).save(output / 'browser-settings.png')
                                popup.destroy()
                        elif module == 'search':
                            read_count = service.catalog.call_count
                            panel.catalog_query.set('101')
                            panel.catalog_query.set('')
                            panel.refresh_catalog()
                            assert service.catalog.call_count == read_count, '未更新的目录应从缓存搜索'
                        root.update()
                        ImageGrab.grab(window=int(root.frame(),16)).save(output / f'{module}-{width}.png')
                    panel.show_module('monitor')
                    panel.risk_filter.set('待核对')
                    root.update()
                    assert len(panel.tree.get_children()) == 2
                    panel.risk_filter.set('全部产品')
                    panel.compact.set(True)
                    panel.refresh()
                    root.update()
                    assert len(panel.tree.get_children()) == 6
                    assert panel.tree.winfo_height() > 100
                # 企业机器人窗口走真实保存流程，发现账号和群的接口全部替换为离线样例。
                from notification_robot_ui import RobotPanel
                from notification_robot_editor import RobotEditor
                target = dict(profile='fixture:one', group_id='cidfixture', group_name='离线样例群',
                              robot_code='dingfixture', robot_name='离线企业机器人')
                client = Mock()
                client.profiles.return_value = [dict(profile='fixture:other',label='其他账号',current=False),
                                                dict(profile='fixture:one',label='当前账号',current=True)]
                client.groups.return_value = [dict(group_id=target['group_id'], group_name=target['group_name'])]
                client.bots.return_value = [dict(robot_code=target['robot_code'], robot_name=target['robot_name'])]
                client.verify.side_effect = lambda value:value
                # 使用左侧真实机器人页保存配置，切换页面后仍需使用同一配置库。
                panel.navigation_buttons['settings'].invoke(); root.update()
                panel.settings_categories['robots'].invoke(); root.update()
                robot_panel = panel.robot_panel
                def settle(editor):
                    deadline = time.monotonic() + 5
                    while editor.winfo_exists() and editor.busy and time.monotonic() < deadline:
                        root.update(); time.sleep(.02)
                    assert not editor.busy, '配置窗口的后台查询未结束'
                    root.update()
                with patch('notification_robot_editor.EnterpriseClient', return_value=client):
                    editor = robot_panel.edit(); settle(editor)
                    assert editor.profile() == 'fixture:one', '不能默认使用第一项账号'
                    editor.query.set('离线样例群'); editor.search(); settle(editor)
                    assert editor.bot_box.current() == 0 and not editor.enabled.get()
                    ImageGrab.grab(window=int(editor.frame(),16)).save(output / 'enterprise-robot.png')
                    editor.save(); settle(editor)
                    saved = robot_panel.store.list_robots()
                    assert len(saved) == 1 and saved[0]['mode'] == 'enterprise' and not saved[0]['enabled']
                    panel.show_module('settings'); panel.show_module('robots'); root.update()
                    assert panel.robot_panel is robot_panel and robot_panel.store.list_robots() == saved
                    editor = robot_panel.edit(saved[0]); settle(editor)
                    editor.name.set('修改备注'); editor.save(); settle(editor)
                    assert robot_panel.store.list_robots()[0]['name'] == '修改备注'
                    editor = robot_panel.edit(); settle(editor)
                    editor.mode.set('Webhook 机器人'); editor.render_mode(); root.update()
                    assert editor.webhook_frame.winfo_ismapped() and not editor.enterprise_frame.winfo_ismapped()
                    ImageGrab.grab(window=int(editor.frame(),16)).save(output / 'webhook-robot.png')
                    editor.close()
                # 验证侧栏实际点击同时调用两项检查；网络路径在离线验收中禁止执行。
                with patch.object(service, 'request_check') as erp_check, patch.object(panel.douyin_runtime, 'check_all',
                        return_value=dict(queued=3,busy=0,skipped=[])) as shop_checks:
                    panel.navigation_buttons['check'].invoke(); root.update()
                    erp_check.assert_called_once(); shop_checks.assert_called_once()
                    assert panel.active_module == 'monitor' and '已排队 3 店' in panel.notice.get()
                ImageGrab.grab(window=int(root.frame(),16)).save(output / 'check-all.png')
                popup = show_coverage(panel, service, 'isolated')
                root.update()
                ImageGrab.grab(window=int(popup.frame(),16)).save(output / 'coverage.png')
                popup.destroy()
                # 真正的 SQL 错误仍应显示红色错误，脚本模式切页也不能触发布局异常。
                normal = service.status()
                for mode, connection in [('sql', 'error'), ('script', 'online')]:
                    simulated = dict(normal, source_mode=mode, connection=connection,
                                     connection_error='模拟连接错误' if connection == 'error' else '模拟脚本提示', error_kind='query')
                    with patch.object(service, 'status', return_value=simulated):
                        for module in ('settings', 'monitor', 'search'):
                            panel.show_module(module); panel.refresh(); root.update()
                            assert not panel._refresh_error
                            if connection == 'error':
                                assert 'SQL 连接或查询失败' in panel.sidebar_erp_status.cget('text')
                            else:
                                assert 'ERP 已连接' in panel.sidebar_erp_status.cget('text')
                # 所有分类均可返回同一设置导航，切换不创建第二套业务对象。
                for category in panel.settings_categories:
                    panel.open_settings(category); root.update()
                    assert panel.settings_context and panel.settings_nav.winfo_ismapped()
                    assert panel.navigation_buttons['settings'].cget('bg') == '#202D44'
                panel.show_module('monitor'); root.update()
                assert not panel.settings_nav.winfo_ismapped()
                panel.refresh()
                text = format_events(service.status()['events'], service)
                (output / '紧凑提醒样例.md').write_text(text, encoding='utf-8')
                assert not errors, errors
                evidence = dict(callback_errors=errors, pages=['monitor','dingtalk','search','config','settings','robots','daily','profit','events','douyin','impact'],
                                navigation_refresh_checks=50, auto_selected_shop=True, stock_filter_verified=True,
                                resolutions=['1380x900','1080x680'], notifications_sent=0, live_connections=0,
                                enterprise_robot_save_edit=True, new_robots_paused=True, robot_modes=['enterprise','webhook'],
                                manual_exclusion_add_remove=True,
                                robot_settings_verified=True, global_check_both=True, shop_manager_count=3,
                                managed_browser_settings_verified=True,
                                patrol_selection_persisted=True, patrol_view_independent=True,
                                coverage=service.status()['coverage']['counts'])
                (output / 'ui-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(evidence, ensure_ascii=False))
        finally:
            if hasattr(panel, 'douyin_runtime'):
                panel.douyin_runtime.close()
            if hasattr(service, 'profit_monitor'):
                service.profit_monitor.stop()
            try:
                panel.destroy()
                root.destroy()
            except tk.TclError:
                pass
            gc.collect()
            if gc_was_enabled:
                gc.enable()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output')
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()
    run(args.output, interactive=args.interactive)
