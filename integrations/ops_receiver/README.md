# 运营机器人同机接收件

在已有钉钉 Stream 的 Windows 主机上，接收端用真实 @ 回调中的机器人、组织、群、成员和消息编号请求本机工作台。固定指令由工作台核验权限；配置目标群的新版订单指令未授权时也不会交给旧代理执行。其余事件沿用原处理。接收件不会创建第二条 Stream 连接。

- 对应的 DWS 独立源码检出固定为提交 50eb73a0906c5911c5c25f9c7106b6ead4f14f62。用本仓库的 Python 运行 apply_dws_hook.py，传入该检出目录，再从该目录运行 go build -trimpath -o dws.exe ./cmd。
- 原 Stream 启动器需给接收端提供 INVENTORY_QUERY_ORG_ID、INVENTORY_QUERY_GROUP_ID、INVENTORY_QUERY_PYTHON、INVENTORY_QUERY_SCRIPT、INVENTORY_QUERY_URL 和独立的 INVENTORY_QUERY_AUTH。URL 是工作台的固定回环端口。凭证由本机部署设施注入，源码和构建产物不包含凭证或真实群标识。
- 将 inventory_query_bridge.py 与工作台同版本的 ops_command_api.py、ops_commands.py、ops_receiver.py、ops_replies.py 配套使用。独立部署时后四个文件位于接收脚本旁的 src 子目录。工作台和接收件要一起更新，因为成员分流探测使用新版工作台提供的本地接口。
- 此接收件处理单次指令回复，并用原会话回复能力核对投递。匹配和巡检通过固定订单后台入队。工作台从持久回执回读，最终结果由同一机器人投递到原配置群；不会缓存临时会话 Webhook。
