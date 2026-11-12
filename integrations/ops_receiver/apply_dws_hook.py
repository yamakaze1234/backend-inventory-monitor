"""将运营指令分流接到独立的 DWS 源码检出；不修改已安装的程序。"""
from pathlib import Path
import shutil
import subprocess
import sys

DWS_COMMIT = '50eb73a0906c5911c5c25f9c7106b6ead4f14f62'
INIT = '\tinventoryQuery := configuredInventoryQueryBridge(clientID)\n'
BLOCK = '''		// 可信事件还必须通过本机成员权限；其他消息走原有 Codex 转发。
		if inventoryQuery.ready() {
			if event, accepted := inventoryQuery.pilot.event(data); accepted && inventoryQuery.member(event) {
				replyEndpoint := data.SessionWebhook
				go func() {
					out, err := inventoryQuery.query(event, "prepare")
					if err != nil || out.Text == "" {
						return
					}
					state := "sent"
					if err := sendInventoryQueryReply(replyEndpoint, out.Text); err != nil {
						// 投递未明确确认，不自动重复发送。
						state = "unknown"
					} else {
						health.onReply()
					}
					if out.Receipt != "" {
						_, _ = inventoryQuery.query(event, state)
					}
				}()
				return []byte(""), nil
			}
		}
'''


def apply(root):
    root = root.resolve(strict=True)
    head = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
    if head != DWS_COMMIT:
        raise SystemExit('DWS 源码版本不匹配，请使用约定的独立检出')
    source = 'internal/helpers/connect_stream.go'
    base = subprocess.check_output(['git','-C',str(root),'show',f'{head}:{source}']).decode('utf-8')
    init = '\tcli := newConnectStreamClient(clientID, clientSecret, keepAlive)\n'
    anchor = '\t\thealth.onPush()\n\t\t// Ack-first:'
    if base.count(init)!=1 or base.count(anchor)!=1:
        raise SystemExit('找不到唯一的 DWS 接入点')
    path = root/source
    modified = base.replace(init,init+INIT).replace(anchor,'\t\thealth.onPush()\n'+BLOCK+'\t\t// Ack-first:')
    if path.read_text(encoding='utf-8') not in (base,modified):
        raise SystemExit('DWS 接收端已有其他改动，请使用独立检出')
    for new in Path(__file__).with_name('dws').glob('*.go'):
        dest = path.parent/new.name
        if dest.exists() and new.read_text(encoding='utf-8')!=dest.read_text(encoding='utf-8'):
            raise SystemExit('DWS 已有其他接入文件，请使用独立检出')
    path.write_text(modified,encoding='utf-8')
    for new in Path(__file__).with_name('dws').glob('*.go'):
        shutil.copyfile(new,path.parent/new.name)


if __name__=='__main__':
    apply(Path(sys.argv[1]))
