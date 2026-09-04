# 从 V1.1.3.1 升级到 V1.1.3.2

1. 停止当前 Uvicorn。
2. 将 V1.1.3.2 增量补丁覆盖到项目根目录。补丁不包含 `.env` 和 `data/digital_goods.db`。
3. 重新启动 Uvicorn。
4. 打开 `GET /api/agent-reach/doctor`，应看到 `opencli.connected=true`。
5. 点击“检测登录并启用 P0”，不应再出现 GBK `UnicodeDecodeError`，且已连接时不应返回 409。

本补丁不要求修改 Windows 系统编码、区域设置或代码页。
