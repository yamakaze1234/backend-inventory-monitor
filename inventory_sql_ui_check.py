"""Offline acceptance for the production SQL/script source UI and reminders."""
import copy
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk


def run(output_directory):
    if not output_directory:raise ValueError('请指定离线验证输出目录。')
    output=Path(output_directory).resolve();output.mkdir(parents=True,exist_ok=True)
    runs=output/'_test_runs';runs.mkdir(exist_ok=True)
    data=Path(tempfile.mkdtemp(dir=runs))
    from inventory_sources import InventoryRuntimeService
    from test_inventory_ui_local import build_workspace
    from monitor_app_control import Controller
    from inventory_delivery import DeliveryWorker
    from sql_inventory_source import warehouse_key
    from PIL import ImageGrab
    import pymssql
    warehouses=[dict(goods_id='101',库房=w,商品编码='DEMO101',商品名称='界面验证商品（模拟数据）',分库数=q,分库可销数=q,分库待入=0)
                for w,q in [('公司大库',8),('样品库',2),('京仓',20)]]
    catalog=[dict(goods_id=str(g),商品编码=s,商品名称=n,总数量=q,可销数=q,待入=0)
             for g,s,n,q in [(101,'DEMO101','界面验证商品（模拟数据）',30),(102,'DEMO102','零库存商品（模拟数据）',0),
                             (103,'EXCLUDED','停售 套餐商品*!',0)]]
    class Connection:
        def cursor(self):return self
        def execute(self,query,params=None):self.rows=copy.deepcopy(warehouses if '分库库存' in query else catalog)
        def fetchmany(self,size):rows,self.rows=self.rows,[];return rows
        def close(self):pass
    service=InventoryRuntimeService(data)
    service.configure_sql(dict(server='offline',port=1433,user='offline',database='ERP'),'offline-only',connector=lambda **_:Connection())
    service.sql_worker.step()
    assert len(service.catalog(limit=None))==2
    service.save_product(dict(sku='DEMO101',goods_id='101',name='界面验证商品（模拟数据）',threshold=0,
                              stock_basis='warehouse_stock',warehouse_id=warehouse_key('样品库'),warehouse_name='样品库'))
    service.request_check();service.sql_worker.step(time.time()+31)
    warehouses[1]['分库数']=7;warehouses[1]['分库待入']=10
    service.request_check();service.sql_worker.step(time.time()+62)
    events=service.status()['events']
    assert {e['kind'] for e in events}=={'stock_increase','pending_inbound'}
    calls=[]
    sender=DeliveryWorker(service,lambda body:calls.append(body) or {'status':'sent'})
    sender.run_once();sender.run_once()
    assert len(calls)==2 and all(e['delivery_status']=='sent' for e in service.status()['events'])
    root=tk.Tk();root.title('后台库存监控 · 正式版 SQL 离线验收');root.geometry('1380x900+20+20')
    root.option_add('*Font',('Microsoft YaHei UI',10))
    errors=[];root.report_callback_exception=lambda kind,error,tb:errors.append(str(error))
    controller=Controller({'workspace':str(data),'portable':True})
    panel=build_workspace(root,service,live=True,dingtalk_controller=controller)
    def capture(name):
        root.update_idletasks();root.update()
        ImageGrab.grab(window=int(root.frame(),16)).save(output/name)
    try:
        panel.show_module('monitor');panel.refresh();capture('sql-monitor.png')
        assert not panel.recovery_bar.winfo_manager()
        panel.show_module('settings');capture('sql-settings.png')
        assert panel.source_panel.mode.get().startswith('SQL')
        assert not panel.script_setup_card.winfo_manager()
        panel.show_module('search');capture('sql-catalog.png')
        assert len(panel.catalog_results.get_children())==2
        panel.show_module('events');capture('reminder-history.png')
        panel.event_page.kind.set('有货待到');panel.event_page.warehouse.set('样品库');panel.event_page.refresh()
        assert '匹配 1 条' in panel.event_page.summary.get()
        assert '待入库' in panel.event_page.text.get('1.0','end')
        assert '当前有 10 件待到货' in panel.event_page.text.get('1.0','end')
        capture('reminder-filter.png')
        service.save_product(dict(sku='DEMO102',goods_id='102',name='零库存商品（模拟数据）',threshold=0))
        catalog[1]['待入']=50
        service.request_check();service.sql_worker.step(time.time()+93)
        product=next(p for p in service.status()['products'] if p['sku']=='DEMO102')
        assert product['pending_quantity']==50 and product['monitor_qty'] is None
        panel.show_module('monitor');panel.refresh();capture('unassigned-pending.png')
        catalog[1]['待入']=60
        service.request_check();service.sql_worker.step(time.time()+124)
        pending=next(e for e in service.status()['events'] if e['sku']=='DEMO102')
        assert pending['warehouse']=='仓库未确认'
        from inventory_delivery import format_events
        assert 'ERP 总待入：60 件 · 仓库未确认' in format_events([pending],service)
        panel.show_module('settings');panel.source_panel.mode.set('脚本查询');panel.source_panel.show_mode();panel.source_panel.apply()
        panel.show_module('settings');panel.refresh();capture('script-settings.png')
        assert service.source_mode()=='script' and panel.recovery_bar.winfo_manager()
        assert panel.script_setup_card.winfo_manager()
        assert not errors,errors
        result=dict(production_ui='passed',sql_default=True,script_switch=True,product_filter=True,
                    reminders_filtered=True,unassigned_pending=True,existing_delivery_mocked=True,real_messages_sent=False,
                    live_sql_connected=False,pymssql=pymssql.__version__,callback_errors=errors)
        (output/'sql-ui-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
    finally:
        root.destroy()
