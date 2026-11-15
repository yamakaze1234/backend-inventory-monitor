"""缓存清理计划：北京时间固定时刻、可选范围、回收站与运行中任务保护。"""
from contextlib import ExitStack, contextmanager, closing
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from pathlib import Path
import re
import sqlite3
import threading
import time

from inventory_cache import SCOPES, cleanup_lock, execute, normalize_scope, preview

TZ=timezone(timedelta(hours=8))
KEY='cache_cleanup_settings'
STATE='cache_cleanup_state'
DEFAULTS=dict(enabled=False,time='03:00',period_days=1,retention_days=30,scopes=list(SCOPES))
RETRY_SECONDS=300
MAX_RETRIES=3
RUN_LEASE_SECONDS=300
_ACTIVE_RUNS=set()
_ACTIVE_LOCK=threading.Lock()


def _number(value,label,minimum,maximum):
    if type(value) is bool or not re.fullmatch(r'[0-9]+',str(value)):
        raise ValueError(label+'必须为整数')
    result=int(value)
    if not minimum<=result<=maximum:raise ValueError(f'{label}需为 {minimum} 至 {maximum}')
    return result


def settings(service):
    with service.db() as db:
        saved=service.get(db,KEY,None)
        state=service.get(db,STATE,{})
    # 默认不启用新的计划；明确保存后才接管旧版导出清理规则。
    return dict(DEFAULTS if saved is None else {**DEFAULTS,**saved},configured=saved is not None,state=state,timezone='Asia/Shanghai')


def legacy_managed(root):
    """旧运行器只读确认是否已由新计划接管，不能误创建另一个部门的数据目录。"""
    path=Path(root)/'inventory.sqlite3'
    if not path.is_file():return False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            return db.execute('SELECT 1 FROM meta WHERE key=?',(KEY,)).fetchone() is not None
    except sqlite3.Error:
        # 无法判断时保留文件，下个空闲周期再读取。
        return True


def next_time(now,at,period_days=1,*,after_run=False):
    current=datetime.fromtimestamp(now,TZ)
    hour,minute=map(int,at.split(':'))
    candidate=current.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if after_run:candidate+=timedelta(days=period_days)
    elif candidate<=current:candidate+=timedelta(days=1)
    return candidate.timestamp()


def save_settings(service,values,now=None):
    now=time.time() if now is None else float(now)
    at=str(values.get('time','')).strip()
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at):
        raise ValueError('清理时间需为北京时间 HH:MM，例如 03:00')
    config=dict(enabled=bool(values.get('enabled')),time=at,
                period_days=_number(values.get('period_days'),'清理间隔天数',1,365),
                retention_days=_number(values.get('retention_days'),'缓存保留天数',0,3650),
                scopes=normalize_scope(values.get('scopes')))
    with cleanup_lock(service.root),service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        service.put(db,KEY,config)
        previous=service.get(db,STATE,{})
        service.put(db,STATE,dict(previous,next_run=next_time(now,at) if config['enabled'] else 0,
                                 retryable=False,attempts=0,scheduled_next=0,owner_pid=0,claim='',lease_until=0,
                                 status='scheduled' if previous.get('status')=='running' else previous.get('status','idle')))
    return settings(service)


class CleanupDeferred(ValueError):
    """正在执行业务时延期；不消费本次计划。"""


@contextmanager
def idle_guard(service,business=None,runtime=None,*,manual=False):
    # 非阻塞获取现有任务锁，避免清理与采集、表单保存互相等待。
    with ExitStack() as stack:
        for owner,name in ((business,'lock'),(getattr(service,'sql_worker',None),'lock'),
                           (runtime,'collection_lock'),(runtime,'job_lock')):
            lock=getattr(owner,name,None)
            if lock is not None:
                if not lock.acquire(blocking=False):raise CleanupDeferred('业务正在运行，缓存清理等待空闲')
                stack.callback(lock.release)
        if business is not None and not manual and business.busy():raise CleanupDeferred('业务操作尚未结束，缓存清理等待空闲')
        if getattr(getattr(service,'sql_worker',None),'busy',False):raise CleanupDeferred('库存正在采集，缓存清理等待空闲')
        if runtime is not None:
            if (getattr(runtime,'pending_collections',set()) or
                    any(job.is_alive() for job in getattr(runtime,'running_jobs',{}).values()) or
                    (getattr(runtime,'commands',None) is not None and not runtime.commands.empty())):
                raise CleanupDeferred('店铺任务尚未结束，缓存清理等待空闲')
        yield


def _owner_alive(pid):
    if not pid:return False
    if os.name=='nt':
        from monitor_app_control import is_process_alive
        return is_process_alive(pid)
    try:os.kill(pid,0);return True
    except OSError:return False


def _due(state,now):
    if state.get('status')!='running':return now>=state.get('next_run',0)
    if now<state.get('lease_until',state.get('started',now)+RUN_LEASE_SECONDS):return False
    with _ACTIVE_LOCK:
        if state.get('owner_pid')==os.getpid() and state.get('claim') in _ACTIVE_RUNS:return False
    # 其他存活进程仍拥有清理权，不能把一次较慢的回收误当作崩溃后重放。
    if state.get('owner_pid') not in (None,0,os.getpid()) and _owner_alive(state['owner_pid']):return False
    return True


def run_due(service,business=None,runtime=None,*,now=None,recycler=None):
    now=time.time() if now is None else float(now)
    config=settings(service)
    if not config['enabled'] or not _due(config['state'],now):return None
    claim=None
    try:
        with idle_guard(service,business,runtime),cleanup_lock(service.root):
            with service.db() as db:
                db.execute('BEGIN IMMEDIATE')
                saved=service.get(db,KEY,{})
                previous=service.get(db,STATE,{})
                if not saved.get('enabled') or not _due(previous,now):return None
                retrying=previous.get('retryable') or previous.get('status')=='running'
                attempts=previous.get('attempts',1)+1 if retrying else 1
                scheduled=previous.get('scheduled_next',0) if retrying else 0
                if not scheduled:scheduled=next_time(now,saved['time'],saved['period_days'],after_run=True)
                while scheduled<=now:scheduled+=saved['period_days']*86400
                if attempts>MAX_RETRIES+1:
                    state=dict(previous,status='failed',finished=now,retryable=False,retry_exhausted=True,
                               next_run=scheduled,errors=['上次清理中断且补试次数已用尽；等待下一正常计划'])
                    service.put(db,STATE,state)
                    return state
                claim=uuid.uuid4().hex
                with _ACTIVE_LOCK:_ACTIVE_RUNS.add(claim)
                service.put(db,STATE,dict(status='running',started=now,lease_until=now+RUN_LEASE_SECONDS,
                    next_run=now+RUN_LEASE_SECONDS,scheduled_next=scheduled,attempts=attempts,
                    owner_pid=os.getpid(),claim=claim))
            try:
                plan=preview(service,saved['retention_days'],now=now,scopes=saved['scopes'])
                result=execute(service,plan,recycler=recycler,now=now)
                state=dict(status='partial' if result['errors'] else 'completed',started=now,finished=time.time(),**result)
            except Exception as error:
                state=dict(status='failed',started=now,finished=time.time(),errors=[str(error) if isinstance(error,ValueError) else '缓存清理失败，文件已保留；请检查本机文件权限'])
            retryable=state['status'] in ('partial','failed') and attempts<=MAX_RETRIES
            state.update(next_run=min(now+RETRY_SECONDS,scheduled) if retryable else scheduled,
                         scheduled_next=scheduled,attempts=attempts,retryable=retryable,
                         retry_exhausted=state['status']!='completed' and not retryable,owner_pid=0,claim='')
            with service.db() as db:service.put(db,STATE,state)
            return state
    except CleanupDeferred:
        return dict(status='deferred',message='业务正在运行，空闲后执行本次缓存清理')
    finally:
        if claim is not None:
            with _ACTIVE_LOCK:_ACTIVE_RUNS.discard(claim)


class CleanupWorker:
    def __init__(self,service,business=None,runtime=None):
        self.service,self.business,self.runtime=service,business,runtime
        self.stop_event=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True,name='cache-cleanup')
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(30):
            try:run_due(self.service,self.business,self.runtime)
            except Exception:pass  # 单次数据库忙不终止后台线程，也不强制删除。

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=15)


def start_worker(service,business=None,runtime=None):
    worker=getattr(service,'cache_cleanup_worker',None)
    if worker is None:
        worker=service.cache_cleanup_worker=CleanupWorker(service,business,runtime)
    return worker


def stop_worker(service):
    worker=getattr(service,'cache_cleanup_worker',None)
    if worker is not None:worker.stop()


def schedule_form(config):
    from inventory_business_views import A,F
    return A('cache-settings','设置定期清理',[F('enabled','启用定期清理','bool'),
             F('time','清理时间（北京时间）','time'),F('period_days','每隔多少天清理','number'),
             F('retention_days','保留天数（0 为全部可清理缓存）','number'),
             F('scopes','自动清理范围','multi',options=list(SCOPES.items()))],
             {k:config[k] for k in DEFAULTS},module='settings',submitLabel='保存计划')


def settings_section(b):
    from inventory_business_views import A,F,S
    config=settings(b.service)
    section=S('cache','本地缓存',planLink='cache')
    section['description']='自定义缓存类型和保留天数；先预览再清理，全部通过回收站。不会删除配置、凭据、数据库文件或未完成提醒。'
    section['forms']=[A('cache-preview','预览清理范围',[F('days','保留天数（0 为全部可清理缓存）','number'),
        F('scopes','清理范围','multi',options=list(SCOPES.items()))],dict(days=config['retention_days'],scopes=config['scopes']))]
    state=config['state'];next_run=state.get('next_run',0)
    next_label=datetime.fromtimestamp(next_run,TZ).strftime('%Y-%m-%d %H:%M') if config['enabled'] and next_run else '未启用'
    status={'running':'处理中','completed':'完成','partial':'部分完成','failed':'失败'}.get(state.get('status'),'尚未执行')
    section['text']='下次清理：'+next_label+'；最近结果：'+status
    if not config['configured']:section['text']+='；保存后统一接管原历史导出清理规则。'
    if state.get('retryable'):section['text']+='；失败后每5分钟补试，最多3次，正常计划时间保持不变。'
    if state.get('retry_exhausted'):section['text']+='；补试次数已用尽，等待下一正常计划。'
    if state.get('errors'):section['text']+='；'+'；'.join(state['errors'])
    return section


def preview_action(b,values):
    from inventory_business_views import A,T
    plan=preview(b.service,_number(values.get('days'),'保留天数',0,3650),scopes=values.get('scopes'))
    token=b.review('cache',plan)
    return dict(message=f"可清理 {plan['row_count']} 条记录 / {plan['file_count']} 个文件",tables=[T('清理范围',[('label','类别'),('rows','记录数'),('files','文件数'),('bytes','字节数')],plan['groups'])],
                forms=[A('cache-confirm','移入回收站',[],dict(token=token),confirm='清理本次预览范围；保留现用配置、最新导出和未完成记录。')])


def execute_action(b,values):
    plan=b.reviewed(values.get('token'),'cache')
    with idle_guard(b.service,b,getattr(b,'douyin',None),manual=True):result=execute(b.service,plan)
    from inventory_business_views import T
    return dict(message='缓存清理完成' if not result['errors'] else '部分缓存清理未完成，剩余内容已保留',tables=[T('清理结果',[('name','项目'),('value','数量')],[dict(name=k,value=v) for k,v in result.items()])])


def save_action(b,values):
    save_settings(b.service,values)
    return dict(message='定期缓存清理设置已保存；程序运行并空闲时按计划执行')
