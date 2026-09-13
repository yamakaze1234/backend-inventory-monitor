# 后台库存监控

Windows 桌面库存与货源监控工具，当前版本为 **正式版 v1.01**。

从已登录 ERP 的浏览器采集库存，在本地 SQLite 中保存产品配置、库存快照及提醒记录，通过钉钉群机器人发送通知。

## 下载使用

到 [Releases](https://github.com/yamakaze1234/backend-inventory-monitor/releases/latest) 下载 `backend-inventory-monitor-v1.01-windows.zip`，解压后运行 `后台库存监控_正式版_v1.01.exe`。安装包内置 Python 和 DWS，不需要另装 Python。

1. 安装随包 `inventory-erp.user.js`，登录 ERP，按页面提示连接采集。
2. 添加关注产品，选择库存依据、仓库及预警数量。
3. 在采集设置中配置自己的钉钉机器人。
4. 如需货源消息监听，在钉钉监控中登录并配置自己的群和人员。首次展示的名称均为示例，请替换。
5. 如需 AI 日报、周报，在货源汇报中配置自己的接口、模型及 Key。

首次使用请看 [中文使用说明](使用说明.md)，库存口径和详细规则见 [功能说明](inventory-setup.md)。

## v1.01 更新

- 库存监控独立运行，钉钉监听按需配置，修复首次配置和子进程资源路径。
- 本机“自动重登（无人值守）”开关；脚本 0.4.0 与 OpenCLI 浏览器点击配合，不读取密码。
- 重登后恢复公司大库并安排新采集；重试失败后一次性机器人断连提醒。
- 完整离线 HTML 教程、统一界面配色、联网一键准备 Node.js/OpenCLI 环境。

Windows 11 x64 + Chrome 已实测，Windows 10 x64 尚未整套验收，Win7 不支持。Edge 自动重登未验收。一键准备不自动确认扩展安装或代填密码；普通库存监控不要求 Node/OpenCLI。

[完整 HTML 教程下载](https://github.com/yamakaze1234/backend-inventory-monitor/releases/download/v1.01/inventory-guide-v1.01.html) · [在线中文教程](使用说明.md)

## 功能

- 重点产品低库存、库存归零、超售及恢复提醒。
- 公司大库可销数或指定分库库存监控，保留数量未知状态。
- 入库单流水识别、库存增加提醒及产品搜索配置。
- 多钉钉机器人、按通知类型选择接收方及独立投递记录。
- 货源消息监听、AI 日报和周报、发送前预览。
- 托盘运行、历史记录、缓存整理和统一滚动界面。

ERP 采集桥接仅监听 `127.0.0.1:18763`。浏览器采集脚本针对现有 3cerp 页面；其他 ERP 需要适配。库存规则不依赖 AI，货源 AI 汇报需要自行配置接口。

## 开发运行

验证环境：Windows、Python 3.13、PowerShell 7、Node.js。Python 需要包含 Tkinter。

在仓库根目录执行：

```powershell
py -3 -m pip install -r requirements.txt
py -3 inventory_app.py
```

`inventory_app.py` 是正式运行入口，配置完成后会按规则发通知。离线界面检查使用：

```powershell
py -3 inventory_app.py --ui-check --data-dir .build/ui-check
```

钉钉功能需要 DWS；源码运行时可将 `dws.exe` 放在 `monitor_assets/`，或加入系统 `PATH`。

## 验证

```powershell
py -3 -m unittest discover -s tests -t . -q
node --test test_inventory_collector.cjs test_inventory_recovery.cjs
```

测试使用临时目录、模拟消息和网络替身。离线 UI 检查不连接 ERP、不启动监听、不发送消息。

## 构建 Windows EXE

准备 DWS 可执行文件，然后在仓库根目录执行：

```powershell
py -3 -m pip install -r requirements-build.txt
pwsh -File ./build_inventory_monitor.ps1 -DwsPath "C:/tools/dws.exe"
```

默认输出为 `dist/backend-inventory-monitor-v1.01/`。已有 EXE 时默认拒绝覆盖；需要重新构建可使用 `-Rebuild`。构建脚本会验证归档依赖并生成 SHA-256 清单。分发时同时附上 `THIRD_PARTY_LICENSES/`。

## 源码结构

| 路径 | 用途 |
| --- | --- |
| `inventory_app.py` | 正式版入口 |
| `inventory_monitor.py`、`inventory_rules.py` | 本地库存存储与规则 |
| `inventory_bridge.py`、`inventory-erp.user.js` | ERP 采集桥接与浏览器脚本 |
| `inventory_panel.py`、`inventory_theme.py`、`*_ui.py` | 桌面界面 |
| `inventory_daily.py`、`inventory_weekly.py`、`inventory_ai.py` | 货源汇报 |
| `monitor_*.py`、`source_monitor_supervisor.py` | 钉钉设置、安装和监听 |
| `notification_*.py`、`inventory_delivery.py` | 机器人与通知投递 |
| `test_inventory_ui_local.py` | 正式入口复用的工作区界面构建函数 |
| `tests/` | Python 回归测试 |
| `packaging/` | PyInstaller 打包配置 |

## 升级与数据

升级前从托盘退出程序，再替换 EXE 和采集脚本。保留程序目录的 `.local_inventory_ui_test/`，以及 `%LOCALAPPDATA%/DingTalkSourceMonitor/` 中原有数据；关闭窗口仅收起到托盘。

本仓库及安装包不包含本机数据库、消息记录、账户登录资料、机器人地址或 AI Key。公开版本将默认群名和人员名替换为示例；库存规则与正式版 v1.01 一致。整理详情见 [发布说明](PUBLICATION.md)，历史功能记录见 [版本记录](版本记录.md)。

第三方组件许可见 [THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES)。
