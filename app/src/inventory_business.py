"""ReUI 业务接口：复用领域服务，界面层不调用 Tk，也不返回已保存的密钥。"""
from __future__ import annotations
import json
import queue
import secrets
import threading
import time
from pathlib import Path

MODULES={'monitor','events','settings','douyin','profit','daily','dingtalk','order-settings','schedules','shipping'}

class BusinessConsole:
    def __init__(self,service,controller=None,douyin=None,offline=False):
        self.service,self.controller,self.douyin=service,controller,douyin
        self.offline=offline
        self.lock=threading.RLock()
        self.jobs={}
        self.reviews={}
        self.latest={}
        self.active=None
        self.cache={}

    def review(self,kind,value):
        key=secrets.token_urlsafe(24)
        with self.lock:
            self.reviews={k:v for k,v in self.reviews.items() if time.monotonic()-v[2]<1800}
            self.reviews[key]=(kind,value,time.monotonic())
        return key

    def reviewed(self,key,kind):
        with self.lock: saved=self.reviews.get(key)
        if not saved or saved[0]!=kind or time.monotonic()-saved[2]>1800:
            raise ValueError('预览已过期，请重新预览后确认')
        return saved[1]

    def busy(self):
        with self.lock:return bool(self.active)

    def snapshot(self,module,query):
        if module not in MODULES:raise ValueError('业务页面不存在')
        from inventory_business_views import build_view
        key=(module,json.dumps(query,sort_keys=True))
        with self.lock:
            cached=self.cache.get(key)
            if cached and time.monotonic()-cached[0]<2:return cached[1]
        result=build_view(self,module,query)
        with self.lock:
            self.cache[key]=(time.monotonic(),result)
            if len(self.cache)>20:self.cache.pop(next(iter(self.cache)))
        return result

    def submit(self,module,action,values,request_id):
        if module not in MODULES or not isinstance(values,dict) or not isinstance(request_id,str) or not 8<=len(request_id)<=80:
            raise ValueError('业务请求无效')
        from inventory_business_actions import ACTIONS
        if (module,action) not in ACTIONS:raise ValueError('不支持的业务操作')
        with self.lock:
            if request_id in self.jobs:return dict(job=request_id)
            if self.active:raise ValueError('上一项业务操作尚未结束，请等待结果')
            self.active=request_id
            self.jobs[request_id]=dict(id=request_id,state='running',module=module,action=action,message='正在处理，请勿重复提交')
        def run():
            try:
                result=ACTIONS[(module,action)](self,values) or {}
                # 导航合并后，连续操作必须继承实际业务模块，不能退回当前页面模块。
                for followup in result.get('forms',[]):followup.setdefault('module',module)
                for listing in result.get('tables',[]):
                    for row in listing.get('rows',[]):
                        for followup in row.get('_forms',[]):followup.setdefault('module',module)
                with self.lock:
                    self.jobs[request_id].update(state='done',message=result.get('message','操作完成'),result=result)
                    self.latest[module]=request_id
            except Exception as error:
                # 领域校验只返回业务提示；意外异常不回传网络响应、连接串或认证数据。
                from monitor_setup_service import SetupError
                message=str(error) if isinstance(error,(ValueError,SetupError)) else '操作未确认完成，请刷新状态后检查，不要重复提交'
                with self.lock:self.jobs[request_id].update(state='failed',message=message)
            finally:
                with self.lock:
                    self.active=None
                    self.cache.clear()
                    while len(self.jobs)>40:self.jobs.pop(next(iter(self.jobs)))
        threading.Thread(target=run,daemon=True,name='business-action').start()
        return dict(job=request_id)

    def job(self,ident):
        with self.lock:
            if ident not in self.jobs:raise ValueError('任务记录已过期，请刷新业务页核对结果')
            return dict(self.jobs[ident])

    def wait_reply(self,reply):
        # 同店长采集可超过 90 秒；回执前保持忙碌，避免退出丢弃排队写入。
        result=reply.get()
        if not result.get('ok'):raise ValueError(result.get('message','业务操作失败'))
        return result

    def robots(self):
        from notification_robots import store_for_service
        return store_for_service(self.service)

    def setup(self):
        if self.controller is None:raise ValueError('当前运行方式未提供钉钉控制器')
        from monitor_setup_service import SetupService
        return SetupService(self.controller.root,portable=True)

    def profit(self):
        if not hasattr(self.service,'profit_monitor'):
            from profit_monitor import ProfitMonitor,CoordinatedProfitSource
            self.service.profit_monitor=ProfitMonitor(self.service.root,
                source_provider=lambda:CoordinatedProfitSource(getattr(self.service,'sql_worker',None)),robots=self.robots())
        return self.service.profit_monitor

    def external(self):
        if self.offline:raise ValueError('隔离候选验收不连接 ERP、浏览器、AI 或发送消息')
