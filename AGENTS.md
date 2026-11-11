# 项目规则入口

- 源码与模块布局见 [README.md](README.md)，结构及数据边界见 [docs/结构与数据流.md](docs/结构与数据流.md)。业务源码在 `app/src`，前端在 `app/web`，统一命令为 `dev.ps1`。
- 开发、提交、推送和 PR 先读 [docs/开发与构建.md](docs/开发与构建.md)；构建发行、签名更新通道或替换现用程序再读 [docs/PUBLICATION.md](docs/PUBLICATION.md)。后续开发遵循功能分支、本地验证和 PR 流程，已有明确授权持续有效。
- 运行数据、维护表、账号与浏览器资料留在本机工作区，不随源码整理、提交或复制。外部订单项目的配置与部署边界见 [integrations/order_backend/README.md](integrations/order_backend/README.md)。
- 面向用户默认简体中文；新增和修改代码添加中文注释。当前源码与本轮验证优先于历史候选说明，完成声明分别记录源码、发行附件、更新通道和本机安装状态。
- 生成文档或报告后只总结并提供必要链接，不自动打开预览。删除文件优先移入回收站；无法使用回收站时先提醒。
