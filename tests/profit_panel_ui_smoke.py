"""Open the real Tk profit panel against an isolated manifest and SQLite store."""
import json
from pathlib import Path
import sys
import time
import tkinter as tk
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from profit_manifest import canonical, sha
from profit_monitor import ProfitMonitor
from profit_panel import ProfitPanel


class Source:
    def __init__(self):
        self.cost = 60000
    def read_profit_costs(self, ids):
        return {'costs': {'123': self.cost}, 'failed': [], 'sourceScope': 'sql:isolated-ui'}


def buttons(frame):
    for child in frame.winfo_children():
        if isinstance(child, tk.Button):
            yield child
        yield from buttons(child)


def run():
    folder = ROOT / '.verification' / ('profit-panel-' + time.strftime('%Y%m%d-%H%M%S'))
    folder.mkdir(parents=True)
    configs = [dict(shopId='intel', configId='c1', productId='p1', product='隔离链接', name='测试配置',
                    priceCents=10000, installment=0, feeCents=0, revision='r1',
                    parts=[dict(lineId='cpu', slot='CPU', name='处理器', goodsId='123', qty=1,
                                taxCents=60000, taxUpdatedAt='2026-09-24T00:00:00Z')])]
    value = dict(contract='profit-monitor/v1', schemaVersion=1, complete=True, sourceId='ui-smoke',
                 workspaceId='ui-smoke-workspace', revision=1, publishedAt='2026-09-24T00:00:00Z',
                 formulaVersion='profit/v1', businessHash=sha(configs), configs=configs)
    value['contentHash'] = sha(value)
    manifest = folder / 'manifest.json'
    manifest.write_text(canonical(value), encoding='utf-8')
    source = Source()
    monitor = ProfitMonitor(folder, source_provider=lambda: source)
    root = tk.Tk()
    from inventory_theme import install_theme
    install_theme(root)
    root.title('ERP 价格监控隔离窗口验收')
    root.geometry('1050x780')
    panel = ProfitPanel(root, monitor)
    panel.pack(fill='both', expand=True)
    root.update()
    commands = {button.cget('text'): button for button in buttons(panel)}
    assert {'绑定', '立即检测', '解除'} <= commands.keys()
    panel.path.set(str(manifest))
    with patch('profit_panel.messagebox.askyesno', return_value=True):
        commands['绑定'].invoke()
    root.update()
    assert monitor.status()['bound']
    assert '已绑定 1 个 ERP 商品 ID' in panel.message.get()
    commands['立即检测'].invoke()
    deadline = time.time() + 8
    def poll():
        if '检测完成' in panel.message.get() or time.time() > deadline:
            root.quit()
        else:
            root.after(50, poll)
    root.after(50, poll)
    root.mainloop()
    assert '检测完成' in panel.message.get(), panel.message.get()
    assert monitor.status()['lastResults'][0]['changed'] == 0
    source.cost += 10000
    commands['立即检测'].invoke()
    deadline = time.time() + 8
    root.after(50, poll)
    root.mainloop()
    assert monitor.status()['lastResults'][0]['changed'] == 1
    summary = panel.summary.get('1.0', 'end')
    assert ('ERP 当前单价' in summary and '处理器' in summary and '达到 100 元' in summary
            and '累计涨 100.00 元' in summary and '测试配置' in summary), summary
    with patch('profit_panel.messagebox.askyesno', return_value=True):
        commands['解除'].invoke()
    root.update()
    assert not monitor.status()['bound']
    assert '已解除旧来源' in panel.message.get()
    print(json.dumps({'window': root.title(), 'boundChecked': True, 'resultsChecked': True,
                      'unbindChecked': True, 'isolatedDir': str(folder)}, ensure_ascii=False))
    root.destroy()
    monitor.stop()


if __name__ == '__main__':
    run()
