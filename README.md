# 后台库存监控

Windows 桌面工具，集中查看 ERP 库存、抖店整机 SKU 库存、补库存建议和通知处理进度。

## 版本与下载

当前源码已同步至 **后台库存监控 v1.06**。公开副本采用与本机一致的分类目录，并将默认连接和通知对象设为空或示例；实际业务逻辑来自本机最新版。本次更新源码，Release 附件另行发布，以发行页实际附件为准。

[查看 Gitea 发行版本](https://gitea.cq360.top/CoreStudio/backend-inventory-monitor/releases) · [GitHub 备用发行源](https://github.com/yamakaze1234/backend-inventory-monitor/releases)

本地开发候选：见 [订单机器人与 ReUI 工作台整合](docs/订单机器人与ReUI工作台整合.md) 、[首轮候选验收](docs/订单整合候选验收.md) 和 [同窗业务页验收](docs/同窗业务页候选验收.md)。尚未发布。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [结构与数据流](docs/结构与数据流.md) | 模块职责、架构图、数据保存边界 |
| [功能与操作](docs/功能与操作.md) | 各模块功能、巡检流程、暂停和缓存说明 |
| [中文使用说明](docs/使用说明.md) | 安装与初始配置 |
| [库存规则](docs/inventory-setup.md) | 库存口径与业务规则 |
| [开发与构建](docs/开发与构建.md) | 依赖安装、测试和打包命令 |
| [发布边界](docs/PUBLICATION.md) | 公开产物与运行数据边界 |

## 快速使用

1. 从发行页下载已发布程序，解压后启动；升级时退出旧程序并保留原数据目录。
2. 配置自己的 ERP 只读库存来源，添加关注产品与预警阈值。
3. 在抖店库存中添加店铺、连接专属浏览器并核对店铺身份，选择主机维护表。
4. 勾选参与巡检的店铺，建立首份官方导出库存基线，再启用定时监控。
5. 配置并启用自己的通知机器人，核对目标群；需要在线明细时单独完成在线表格配置。

运行数据、人工备注、数据库连接及浏览器登录资料不属于源码。不要把运行目录整体上传仓库。第三方许可见 [THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES)。

## 目录

```text
app/
  src/       程序源码
  ui/        图标与浏览器脚本
  runtime/   启动入口
  data/      运行时产生，本仓库不提供业务数据
tools/       检查与构建脚本
  packaging/ PyInstaller 配置
tests/       Python 与浏览器脚本测试
docs/        功能、结构、使用和发布说明
THIRD_PARTY_LICENSES/ 第三方许可
启动.cmd     桌面启动入口
dev.ps1      统一开发命令
```
