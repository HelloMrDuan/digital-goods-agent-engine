# V1.1.3.2 → V1.1.3.3

1. 停止当前 Uvicorn。
2. 将 `digital-goods-v1.1.3.3-windows-opencli-hotfix-patch.zip` 解压并覆盖项目根目录。
3. 不要覆盖/删除现有 `.env` 与 `data/digital_goods.db`。补丁不携带这两个生产文件。
4. 重新启动 Uvicorn。
5. 保持 Chrome + OpenCLI Browser Bridge 连接，直接点击“采集核心需求”。页面会以 force=true 手工重试，不必等待旧 BACKOFF 到期。

本补丁只修 Windows CLI 命令解析；不会插入任何模拟业务数据。
