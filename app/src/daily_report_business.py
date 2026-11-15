"""日报设置与固定诊断，沿用桌面表单，不读取凭据文件。"""
import json
import os
from pathlib import Path
import subprocess
import daily_report_config as config
import order_bot_config
from inventory_business_views import A,F,S


def view(workspace):
    section=S('daily-report','日报数据库配置')
    try:
        value=config.load(workspace) or {}
        section['text']='统计项目组：抖英特旗舰组；只读；内网直连。'+('已配置' if value else '待配置')
    except ValueError as error:
        value={};section['text']=str(error)
    section['forms']=[A('daily-report-save','配置日报连接',[
        F('database','数据库',readonly=True),
        F('dbxExecutable','DBX MCP 可执行文件'),F('dbxData','DBX 数据目录'),F('python','Python 可执行文件')],
        dict(value,database='ERP'),description='只填写连接引用和运行路径；账号、密码及服务器地址在 DBX 管理。保存不会重启机器人。'),
        A('daily-report-import','导入非敏感配置',[F('configJson','配置 JSON','textarea')],description='仅导入配置引用，不迁移 DBX 凭据；导入后请重新测试连接。'),
        A('daily-report-test','测试连接',immediate=True),
        A('daily-report-generate','生成测试日报',[F('date','已结束自然日','date')])]
    return section


def action(b,operation,value):
    root=b.service.root
    if operation=='import':
        try: imported=json.loads(value.get('configJson',''))
        except (ValueError,TypeError):raise ValueError('配置 JSON 无效') from None
        config.save(root,imported)
        return dict(message='配置引用已导入，迁移仍待连接测试；DBX 凭据未复制')
    if operation=='select':
        plan=b.reviewed(value.get('token'),'daily-report-connection')
        rows=[r for r in plan['connections'] if r['id']==value.get('connectionId')]
        if len(rows)!=1:raise ValueError('连接选择失效，请重新读取')
        row=rows[0]
        config.save(root,dict(plan['runtime'],connectionId=row['id'],connectionName=row['name'],database='ERP',version=1,networkMode='direct'))
        return dict(message='日报连接已绑定；新任务生效。服务器：'+row['host']+':'+row['port'])
    b.external()
    settings=order_bot_config.load(root)
    if not settings:raise ValueError('请先配置机器人运行环境')
    if operation!='save':config.load(root)
    script=Path(settings['control_root'])/'daily-report-command.mjs'
    if not script.is_file():raise ValueError('请先安全安装新版兼容后端')
    env=dict(os.environ,ORDER_BOT_CONFIG=str(order_bot_config.config_path(root).resolve()))
    command=[settings['node_path'],str(script),'discover' if operation=='save' else operation]
    runtime={k:value.get(k,'') for k in ('dbxExecutable','dbxData','python')}
    if operation=='save' and any(not Path(v).is_absolute() for v in runtime.values()):raise ValueError('请选择绝对运行路径')
    if operation=='report':command.append(str(value.get('date','')))
    try:
        result=subprocess.run(command,input=json.dumps(runtime) if operation=='save' else None,env=env,capture_output=True,text=True,encoding='utf-8',timeout=300,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        response=json.loads(result.stdout)
    except (OSError,ValueError,subprocess.TimeoutExpired):raise ValueError('日报诊断未完成，请检查运行环境') from None
    if not response.get('ok'):raise ValueError(response.get('message','日报检查失败'))
    if operation=='save':
        rows=response.get('connections',[])
        if not rows:raise ValueError('DBX 未配置 SQL Server 连接')
        token=b.review('daily-report-connection',dict(runtime=runtime,connections=rows))
        return dict(message='请选择已保存的日报只读连接',forms=[A('daily-report-select','绑定日报连接',[F('connectionId','连接及服务器',options=[(r['id'],r['name']+' · '+r['host']+':'+r['port']) for r in rows])],dict(token=token))])
    return dict(message=response['message']+('；目录：'+str(Path(response['files'][0]).parent) if response.get('files') else ''))
