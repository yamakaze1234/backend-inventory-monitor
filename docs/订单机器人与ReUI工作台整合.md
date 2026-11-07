# 订单机器人与 ReUI 工作台整合

开发分支：`codex/order-bot-reui-integration`。本轮为本地候选开发，不表示已推送、发布或替换现用程序。

## 使用与边界

启动候选程序后，在独立 WebView2 桌面窗口打开由同一进程提供的本机工作台。库存监控、订单匹配、软件更新使用 React 和 ReUI；抖店库存、钉钉监控、货源汇报、价格监控、商品管理及连接设置仍通过侧栏打开原生管理窗口。原生界面可使用 `--legacy-ui` 启动。关闭工作台窗口不停止后台；通过托盘退出程序。

订单页面复用 `D:\工作\匹配\抖店SKU查询\抖店SKU查询` 的原匹配后端和 `D:\工作\PythonProject\数据分析\机器人日报` 的控制入口。队列仍在 `D:\工作\匹配\巡检运行数据\群机器人`，不复制业务数据库或登录资料。因此新增功能当前依赖这台机器已有的匹配运行环境，不是可单独搬到其他电脑使用的订单机器人安装包。

页面提供启动、停止、暂停/恢复自动巡检、当天巡检、订单/SPU/号码匹配、任务分页和原格式结果。正常匹配继续遵守店铺＋SPU＋SKU 唯一性；排队、执行、ERP 回读和消息投递分别展示。打开工作台不会自动启动匹配或发群消息。通过本次工作台启动的订单服务在退出时安全停止；启动前已独立运行的服务不由普通退出接管。

表格默认每页 25 条，任务记录在数据库分页，库存视图按稳定 SKU 缓存和过滤。请求不重叠，页面隐藏时暂停轮询，相同快照不重复更新 React 状态。前端只使用本机同源服务；写接口检查 Host、Origin 和随机会话头。

## 更新

支持启动时自动检查，以及“检查更新 → 一键更新并重启”。不会未经用户点击安装新版本。发布源为本项目 Gitea，GitHub 为备用源；使用固定 `inventory-updates` 通道的 `inventory-update.json`。

清单必须通过内置 Ed25519 公钥验证，包含应用、平台、正式版本、附件地址、大小和 SHA-256。更新只替换 EXE，旧程序保留为同目录 rollback 文件。下载/验签失败保留旧程序；替换前失败重启旧程序；新版启动退出且未确认健康时回滚。新版仍存活但健康未确认时不强杀业务进程，保留回滚文件及失败记录。自定义 `--data-dir`、托盘/原生界面参数会继续使用。

安装前禁止新工作台操作，拒绝排队、运行中或结果未知的匹配任务，等待原订单执行进程与监听安全停止。补偿与启动恢复意图只消费一次，失败时显示具体服务需手动检查，不因旧恢复标记再次启用用户后来暂停的服务。

`tools/prepare_update_manifest.py` 只生成签名清单，不上传。签名私钥位于发布者工作区之外，不随 Git 或 EXE 交付。首次上线仍须按既有授权流程发布候选对应的正式版本、两个源的同一附件和签名清单，再进行真实通道下载及升级回读。本轮不创建更新通道、不发布附件。

## 开发与隔离验收

```powershell
npm --prefix app/web ci
./dev.ps1 WebBuild
./dev.ps1 WebPreview
```

`WebPreview` 仅使用 `outputs/订单整合验收/preview-data` 的样例库存与订单，不启动 ERP、机器人或通知。打包候选可传 `--console-preview-data <独立目录>` 做同样验收，预览地址写在该目录 `preview-url.txt`，其中临时会话值不得作为生产配置提交。

Python 新增测试：`test_order_console.py`、`test_inventory_updater.py`；浏览器验收工具：`tools/verify_console_ui.cjs`；更新 EXE 替换验收：`tools/verify_update_helper.ps1`。后者只创建和运行本轮临时样例 EXE。测试临时目录集中在输出目录，收尾移入回收站。

独立工作树构建可通过 `tools/build_inventory_monitor.ps1` 的 `-DependencyRoot` 复用既有 Python 打包依赖，通过 `-IndependentRoot` 指定既有独立工具源码位置。参数只解析构建依赖，不迁移现用业务数据。

ReUI 来源：[分页示例](https://reui.io/preview/base/components/c-data-grid-1?ref=mcp)、[Frame](https://reui.io/components/frame?ref=mcp)、[Badge](https://reui.io/components/badge?ref=mcp)。保留官方组件样式，业务层不添加入场动画。原生管理窗口尚未转换为 ReUI，真实 ERP 写入、群投递、生产升级通道未作为本轮隔离验证的通过项。

不使用电脑操作的候选包验收可传 `--console-preview-data <独立目录> --console-headless --console-preview-seconds 60`。该模式不创建窗口，自动退出，并验证包内 WebView2 宿主运行库。
