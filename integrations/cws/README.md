# 技嘉店铺 CWS 试点

此目录提供独立试点脚本，不改变正式工作台的采集引擎、Harness、数据库连接或店铺计划。仅针对已授权的技嘉旗舰店，核对页面店名后导出商品管理「全部」范围，复用官方表数量、字段与整机类目校验。结果留在 CWS 工作目录，未自动写回现用库存。

## 配置

1. 使用独立 CWS 工作目录、浏览器数据目录和未占用的 CDP 端口。安装 core-workspace-cli、scraper 及 openpyxl；Python 版本以 CWS 要求为准。登录由使用者完成，不复制原浏览器凭据。
2. 将 `DY-技嘉-官方库存巡检.py` 放到该工作目录的 `config/plugins/scraper/scripts/DY/库存/`。
3. 将本仓库 `app/src` 下的 `douyin_cws_export.py`、`douyin_export.py`、`douyin_managed_export.py`、`douyin_browser.py`、`douyin_inventory.py`、`douyin_scope.py` 复制到 `config/plugins/scraper/support/`。支持模块不能放进脚本扫描目录。
4. 先通过 `scripts --tag 巡检` 和 `profile list` 确认实际脚本与 Profile。运行时传入 `--jobs 1`，并通过 `--options` 的 `状态目录` 指定独立绝对路径。发现、登录、运行均使用同一 `--directory`、`--data-dir` 和 `--port`。
5. 回读结果 JSON 的 `records`，核对店名、商品与 SKU 数量、库存和报表时间。连续执行须生成不同时间的新报表；失败后保留任务文件，续跑原报表而非重新提交。

## 下载兼容

本轮 CWS 的 Playwright CDP 入口使用 `no_defaults=True`，默认下载辅助在真实 Edge 上收到开始事件后未结束等待。适配器按本页 frame、当前 browserContextId 和下载 GUID 监听完成事件，最多等待 60 秒，并将文件保存在 CWS 注入的本轮下载目录。不会从公共下载目录按文件名猜测文件；结束后恢复浏览器默认下载行为。

本试点仅串行运行，采集期间关闭页面调试连接，避免不同控制器更改同一浏览器上下文。尚未实现通用多店铺调度适配，不能将单店验证表述为全平台迁移完成。

## 自动巡检失败提醒

正式工作台新增独立通知类型 `douyin_patrol_failure`，默认不订阅。启用后，仅定时巡检/重启续取的最终失败或在线同步不完整时发送；手动、取消、成功、内部等待和内部恢复重试不触发。每轮按独立任务号和接收方持久记录，失败/未知投递均不自动补发。

提醒包含店铺、结束时间、耗时、脱敏原因、影响及处理建议。快照已保存后同步失败使用不同说明。配置保存于原通知数据库，升级 EXE 不覆盖该订阅或其他业务配置。
