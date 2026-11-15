"""统一的迁移与桌面连接入口：检查、配置凭据、启动或停止。"""
import argparse
import json
import os
from pathlib import Path
import subprocess

from order_department_runtime import settings
import harness_runtime as runtime


def execute(action):
    config = settings()
    if runtime.connector_mode(config) != 'harness':
        raise ValueError('此工作区尚未选择 DeepSeek Harness，请先运行一键配置')
    workspace = Path(os.environ['ORDER_BOT_CONFIG']).parent
    if action == 'Stop':
        if not runtime.stop(config):
            raise ValueError('未能停止 Harness；请从原入口退出并核对状态')
        return dict(state='down')
    if action in ('Configure', 'Check'):
        control, home, entry = runtime.layout(config)
        target = entry.parents[4]/'node_modules/@dingtalk-real-ai/dsh-dingtalk/lib/bin.js'
        if action == 'Configure':
            if runtime.status(config)['state'] != 'down':
                raise ValueError('请先停止 Harness Web 再配置钉钉')
            env = dict(os.environ, DSH_HOME=str(home), STOCK_MONITOR_DSH_ENTRY=str(entry),
                       STOCK_MONITOR_DSH_CWD=str(control),
                       PATH=os.pathsep.join((str(Path(config['node_path']).parent),
                                            str(entry.parents[4]/'node_modules/.bin'), os.environ.get('PATH',''))))
            # 登录资料由插件在本机交互输入，不经过迁移脚本。
            code = subprocess.call([config['node_path'],str(target),'setup'],cwd=control,env=env)
            if code:
                raise ValueError('钉钉连接器设置未完成')
            return dict(configured=True)
        from robot_desktop_control import connector_status, get_config, bot
        from harness_bridge import probe
        selected = get_config(bot.STATE)
        current = connector_status()
        try:
            probe(workspace, selected['profile'].partition(':')[0], selected['robotCode'])
            current['commandApiReady'] = True
        except (OSError,ValueError,KeyError,TypeError):
            current['commandApiReady'] = False
        return current
    from robot_desktop_control import bot, get_config
    pid = runtime.start(config, get_config(bot.STATE), workspace)
    return dict(state='starting' if pid else 'already_running')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('Configure','Check','Start','Stop'))
    try:
        print(json.dumps(execute(parser.parse_args().action), ensure_ascii=False))
    except (OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired) as error:
        print(str(error) if type(error) is ValueError else 'Harness 操作未完成，请核对运行目录、本机配置与启动状态。')
        raise SystemExit(1) from None
