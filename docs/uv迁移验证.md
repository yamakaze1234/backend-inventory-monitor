# uv 迁移验证（2026-11-11）

本轮仅迁移开发工具链；应用版本仍为 v1.07。分支 `codex/uv-development`，基线为 Gitea main 的 `4e6945c`。未更新现用 EXE、正式附件或更新通道。

## 改动

- 新增 pyproject.toml、uv.lock、.python-version；声明 Windows x64 / Python 3.13，默认解释器3.13.6，锁定18个外部包。
- 开发、测试、源码启动及直接打包使用同一 uv 项目环境，锁过期即失败。实际验证中文路径可正常返回解释器。
- 一键开发部署安装uv并按锁文件同步，前端继续npm ci；Runtime不要求uv。只读检查同时核对环境和锁文件，兼容Windows PowerShell5.1引导。
- requirements保留为兼容导出，依赖只在pyproject.toml维护；构建清单记录uv配置和锁文件摘要。

## 本轮本地证据

- `uv lock --check`、`uv sync --locked`：通过；从空项目环境安装18个包。
- `./dev.ps1 Check`：168个Python文件语法与资源通过。
- `./dev.ps1 Test`：601项Python测试、21项浏览器脚本测试通过。
- `./tools/prepare-environment.ps1 -Mode Development`：实际uv同步和npm ci完成，安装后检测通过；PS7和系统PS5.1下的开发CheckOnly均通过。
- `./tools/verify_environment_setup.ps1`：运行模式不装uv、开发模式选uv、只读与幂等、安装后回读及失败处理通过。
- 隔离修改six依赖声明后执行`uv sync --locked --dry-run`：拒绝过期锁，原锁文件未改、未创建虚拟环境。
- `./tools/build_inventory_monitor.ps1 -OutputDirectory outputs/uv-migration/package`：uv环境构建成功，归档依赖检查通过。
- 候选包界面验证：分页搜索、7个导航25分区、390px布局、更新入口、27个只读数据集与权限隔离通过。

以上是本机验证，不是服务器CI；没有在空白Windows上重新安装uv或下载Python，也没有运行真实业务写入或通知投递。
