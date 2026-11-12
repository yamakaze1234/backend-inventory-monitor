"""部门订单机器人独立配置与兼容部署，不读取凭据，不隐式接管其他队列。"""
from contextlib import closing
from functools import lru_cache
import runpy
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

PATH_FIELDS=('match_root','control_root','state_root','python_path','dws_path','pwsh_path','node_path')
PUBLIC_TARGET=('robotName','robotCode','appId','groupName','groupId','profile','intervalMinutes')


def config_path(workspace):return Path(workspace)/'order-bot.json'


def load(workspace):
    path=config_path(workspace)
    if not path.is_file():return {}
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        if value.get('version')!=1:raise ValueError()
        return value
    except (ValueError,OSError):raise ValueError('订单机器人配置无法读取，请检查本部门配置文件') from None


def queue_state(root):
    """只读公开状态，不调用会自动建表或发送历史消息的后端 status。"""
    path=Path(root)/'jobs.sqlite3'
    if not path.is_file():return dict(active=0,online=False,enabled=False,target={})
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        values=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('heartbeat','serviceEnabled','robotConfig','pendingRobotConfig','matchPatrolWindow')"))
        active=db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','unknown') OR delivery IN ('pending','unknown')").fetchone()[0]
    target=json.loads(values.get('robotConfig') or '{}')
    return dict(active=active,online=time.time()-float(values.get('heartbeat','0'))<90,
                enabled=values.get('serviceEnabled','0')=='1',switching=bool(values.get('pendingRobotConfig')),
                target={key:target.get(key) for key in PUBLIC_TARGET if key in target},
                patrolWindow=patrol_window_view(values.get('matchPatrolWindow'),target.get('intervalMinutes')))


def assert_stopped(root):
    state=queue_state(root)
    if state['active'] or state['online'] or state['enabled'] or state.get('switching'):
        raise ValueError('请先停止订单匹配服务，并处理在途或投递未确认任务，再修改运行环境')


def assert_state_owner(workspace,root):
    """队列所属独立检查，不能凭当前工作区部署过就接管另一个部门。"""
    root=Path(root);owner=root/'order-owner.json'
    if owner.is_file():
        value=json.loads(owner.read_text(encoding='utf-8'))
        if value.get('configPath')!=str(config_path(workspace).resolve()):
            raise ValueError('所选队列属于其他部门，请选择独立队列目录')
    elif any(p.exists() for p in (root/'agent/.codex/config.toml',root/'agent/AGENTS.md')):
        raise ValueError('所选队列已有独立机器人配置，请选择空的部门队列目录')


def assert_connector_stopped(config,root=None):
    """停止请求不是停止完成；只接受 DWS 明确回读的 down 状态。"""
    root=Path(root or config['state_root'])
    if not (root/'jobs.sqlite3').is_file():return
    target=queue_state(root)['target']
    if not target.get('robotCode') or not target.get('profile'):
        raise ValueError('旧队列缺少机器人路由，无法确认连接器已停止，请选择独立队列目录')
    try:
        result=subprocess.run([config['dws_path'],'dev','connect','status','--robot-client-id',target['robotCode'],'--profile',target['profile'],'--json','--format','json'],capture_output=True,text=True,encoding='utf-8',timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        value=json.loads(result.stdout)
        if result.returncode or value.get('error') or value.get('state')!='down':raise ValueError()
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise ValueError('订单 Stream 连接器尚未确认停止，请先在原程序完成停止再部署或修改环境') from None


def normalize(values):
    if not isinstance(values,dict):raise ValueError('订单运行配置格式无效')
    clean=dict(version=1)
    for key in PATH_FIELDS:
        value=values.get(key,'')
        if not isinstance(value,str) or not value.strip() or not Path(value).is_absolute():
            raise ValueError('请填写完整的订单后端目录与运行程序路径')
        clean[key]=str(Path(value).resolve())
    if len({os.path.normcase(clean[k]) for k in ('match_root','control_root','state_root')})!=3:
        raise ValueError('匹配源码、控制源码和队列目录必须分别设置')
    for key in ('python_path','dws_path','pwsh_path','node_path'):
        if not Path(clean[key]).is_file():raise ValueError('运行程序不存在：'+key)
    match=Path(clean['match_root'])/'app/src'
    control=Path(clean['control_root'])
    for file in (match/'group_order_bot.py',match/'patrol_worker.py',match/'batch_match.py',control/'robot_desktop_control.py',control/'robot_record_cleanup.py'):
        if not file.is_file():raise ValueError('订单匹配后端文件缺失：'+file.name)
    rows=values.get('shops')
    if not isinstance(rows,list) or not 1<=len(rows)<=200:raise ValueError('请选择 1 至 200 个本部门店铺')
    shops=[];seen=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('店铺格式无效')
        ident=str(row.get('id','')).strip();name=str(row.get('name','')).strip()
        if not re.fullmatch(r'[1-9][0-9]{0,19}',ident) or not name or len(name)>100 or ident in seen:
            raise ValueError('店铺编号、名称无效或重复')
        shops.append(dict(id=ident,name=name));seen.add(ident)
    clean['shops']=shops
    # 部门范围必须有明确维护表绑定，禁止沿用匹配核心内个人默认文件路径。
    binding=Path(clean['match_root'])/'app/data/维护表设置.json'
    try:
        profiles=json.loads(binding.read_text(encoding='utf-8-sig'))
        if profiles.get('version')!=1:raise ValueError()
        for shop in shops:
            row=profiles['shops'][shop['id']]
            if not all(isinstance(row.get(k),str) and row[k].strip() for k in ('name','path','sheet')):raise ValueError()
            if not Path(row['path']).is_absolute():raise ValueError()
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        raise ValueError('请先在匹配项目维护表设置中，为每个部门店铺保存完整维护表路径和工作表') from None
    model=str(values.get('agent_model') or 'deepseek/deepseek-flash').strip()
    if not re.fullmatch(r'[A-Za-z0-9._:/-]{1,150}',model):raise ValueError('订单机器人模型标识无效')
    clean['agent_model']=model
    return clean


def save(workspace,values):
    clean=normalize(values);old=load(workspace)
    assert_state_owner(workspace,clean['state_root'])
    if old and old!=clean:
        assert_stopped(old['state_root']);assert_connector_stopped(old)
    if old!=clean:
        assert_stopped(clean['state_root']);assert_connector_stopped(clean)
    # 不覆盖别的部门已有部署所属；普通保存也不能把现用后端重新指向其他工作区。
    for parent in (Path(clean['match_root'])/'app/src',Path(clean['control_root'])):
        pointer=parent/'order-department.json'
        if pointer.is_file():
            owned=json.loads(pointer.read_text(encoding='utf-8')).get('configPath')
            if not owned or Path(owned).resolve()!=config_path(workspace).resolve():
                raise ValueError('所选订单后端已绑定其他部门，请选择独立安装目录')
    path=config_path(workspace);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.pending');temporary.write_text(json.dumps(clean,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)
    return clean


def bundle_root():
    return (Path(sys._MEIPASS)/'order_backend') if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[2]/'integrations/order_backend'


@lru_cache(maxsize=1)
def patrol_window_tools():
    # 桌面和订单进程共用兼容包中的窗口规则，避免默认时段或边界不一致。
    return runpy.run_path(str(bundle_root()/'matching/order_patrol_window.py'))


def patrol_window_view(raw, legacy_interval=None):
    tools=patrol_window_tools()
    return tools['describe'](raw,time.time(),legacy_interval or tools['DEFAULT_INTERVAL'])


def patrol_window_ready(config):
    return bool(config.get('match_root') and (Path(config['match_root'])/'app/src/order_patrol_window.py').is_file())


def deployed(workspace,config=None):
    value=config or load(workspace)
    if not value:return False
    marker=Path(workspace)/'order-backend-install.json'
    if not marker.is_file():return False
    try:
        saved=json.loads(marker.read_text(encoding='utf-8'))
        if saved['configPath']!=str(config_path(workspace).resolve()) or saved.get('config')!=value:return False
        for row in saved['files']:
            path=Path(row['path'])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:return False
        return True
    except (OSError,ValueError,KeyError):return False


def deploy(workspace):
    """显式部署到选定后端，保留原文件备份，不停止或覆盖运行中的业务。"""
    config=load(workspace)
    if not config:raise ValueError('请先保存订单运行环境')
    normalize(config);assert_stopped(config['state_root']);assert_connector_stopped(config)
    source=bundle_root()
    targets={'matching':Path(config['match_root'])/'app/src','control':Path(config['control_root'])}
    # 已部署标记用于允许安全停止旧版本；更新部署另外比较候选附带的源码。
    if deployed(workspace,config) and all((parent/file.name).is_file() and file.read_bytes()==(parent/file.name).read_bytes()
        for kind,parent in targets.items() for file in (source/kind).iterdir() if file.is_file() and file.suffix in ('.py','.pyw','.mjs','.ps1')):
        return dict(message='订单兼容后端已部署',changed=False)
    # 旧版有固定 STATE 时也必须检查原队列，不能借新空目录绕过停机。
    legacy=targets['matching']/'group_order_bot.py'
    tree=ast.parse(legacy.read_text(encoding='utf-8-sig'))
    for statement in tree.body:
        if isinstance(statement,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='STATE' for t in statement.targets):
            value=statement.value
            if isinstance(value,ast.Call) and value.args and isinstance(value.args[0],ast.Constant) and isinstance(value.args[0].value,str):
                assert_stopped(value.args[0].value)
                assert_connector_stopped(config,value.args[0].value)
    agent=Path(config['state_root'])/'agent'
    marker=Path(workspace)/'order-backend-install.json'
    assert_state_owner(workspace,config['state_root'])
    changes=[]
    for kind,parent in targets.items():
        pointer=parent/'order-department.json'
        if pointer.is_file() and Path(json.loads(pointer.read_text(encoding='utf-8'))['configPath']).resolve()!=config_path(workspace).resolve():
            raise ValueError('后端已经由其他部门配置，未覆盖')
        for file in (source/kind).iterdir():
            if file.is_file() and file.suffix in ('.py','.pyw','.mjs','.ps1'):changes.append((file,parent/file.name,kind))
    if not changes:raise ValueError('订单兼容源缺失，请检查安装包')
    backup=Path(workspace)/'order-backend-backups'/uuid.uuid4().hex;backup.mkdir(parents=True)
    records=[]
    # 路由指针和代理配置也属于部署文件，升级时同样留备份。
    extras=[*(parent/'order-department.json' for parent in targets.values()),agent/'.codex/config.toml',agent/'AGENTS.md',Path(config['state_root'])/'order-owner.json']
    for index,target in enumerate(extras):
        if target.is_file():
            old=backup/'extra'/str(index);old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,old)
        records.append(dict(path=str(target),existed=target.is_file(),backup=str(backup/'extra'/str(index))))
    for source_file,target,kind in changes:
        if target.is_file():
            old=backup/kind/target.name;old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,old)
        records.append(dict(path=str(target),existed=target.is_file(),kind=kind,backup=str(backup/kind/target.name)))
    (backup/'original-files.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        # 文件已备份后才一次应用兼容层；部署不自动启动连接器或匹配。
        for source_file,target,kind in changes:shutil.copy2(source_file,target)
        pointer=json.dumps(dict(configPath=str(config_path(workspace).resolve())),ensure_ascii=False)
        for parent in targets.values():(parent/'order-department.json').write_text(pointer,encoding='utf-8')
        agent=Path(config['state_root'])/'agent';(agent/'.codex').mkdir(parents=True,exist_ok=True)
        (Path(config['state_root'])/'order-owner.json').write_text(pointer,encoding='utf-8')
        # 独立工作目录只注册订单工具，不继承某部门日报、数据库或聊天范围。
        literal=lambda value:json.dumps(str(value),ensure_ascii=False)
        toml='[mcp_servers.erp_order_matching]\ncommand = '+literal(config['node_path'])+'\nargs = ['+literal(Path(config['control_root'])/'order-match-server.mjs')+']\nstartup_timeout_sec = 30\ntool_timeout_sec = 45\n[mcp_servers.erp_order_matching.env]\nORDER_BOT_CONFIG = '+literal(config_path(workspace).resolve())+'\n'
        (agent/'.codex/config.toml').write_text(toml,encoding='utf-8')
        (agent/'AGENTS.md').write_text('订单匹配只使用 erp_order_matching 工具。仅在用户明确要求匹配时提交；默认使用网店单号，号码保持字符串并保留完整后缀。用户明确提供 NO- 开头的 ERP 单号时可按该单号查找。店铺不明确时由后端识别。queued 只表示入队；未知写入不得重试。收到查询状态请求时原样返回 replyText，不把网店单号改写为 ERP 单号。重新匹配需用户明确提出，不从普通匹配推断。\n',encoding='utf-8')
        result=dict(configPath=str(config_path(workspace).resolve()),config=config,backup=str(backup),files=[dict(path=str(target),sha256=hashlib.sha256(target.read_bytes()).hexdigest()) for _,target,_ in changes])
        for extra in extras:
            result['files'].append(dict(path=str(extra),sha256=hashlib.sha256(extra.read_bytes()).hexdigest()))
        (backup/'original-files.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        (Path(workspace)/'order-backend-install.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception:
        # 失败时恢复原文件；新增文件移到失败备份中，不直接删除。
        for index,row in enumerate(reversed(records)):
            target=Path(row['path'])
            if row['existed']:
                shutil.copy2(row['backup'],target)
            elif target.is_file():
                failed=backup/'failed'/str(index);failed.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(str(target),str(failed))
        raise
    return dict(message='订单兼容后端已部署，原文件已备份；尚未启动',changed=True,backup=str(backup))


def describe(workspace):
    config=load(workspace)
    defaults={key:'' for key in PATH_FIELDS};defaults.update(agent_model='deepseek/deepseek-flash',shops=[])
    if not config:return dict(configured=False,deployed=False,**defaults,target={},message='请先单独配置订单匹配后端、店铺及 Stream 机器人')
    state=queue_state(config['state_root'])
    return dict(configured=True,deployed=deployed(workspace,config),**config,target=state['target'],active=state['active'],online=state['online'],patrolWindow=state.get('patrolWindow'),
                patrolWindowReady=patrol_window_ready(config),message='订单匹配与库存通知使用独立配置')
