# 从 V1.1.1 升级到 V1.1.2 Business Priority

推荐直接覆盖代码文件，保留你当前的 `.env` 和 `data/digital_goods.db`。

1. 停止当前 Uvicorn。
2. 备份 `data/digital_goods.db`。
3. 用 V1.1.2 的 `app/`、`web/`、`scripts/` 覆盖旧目录；同步 `README.md / RELEASE_NOTES.md / VALIDATION.md` 可选。
4. 不要覆盖旧 `.env`，也不要删除旧 `data/digital_goods.db`。
5. 启动：`python -m uvicorn app.main:app --host 127.0.0.1 --port 8080`
6. 进入“采集中心”，点击 **同步赚钱优先推荐源**。

同步后：

- 旧版三个 V2EX 默认源会被降为 `P3 / auxiliary` 并关闭；
- 旧 V2EX evidence 仍保留，方便追溯，但不会进入 V4 商业需求聚类；
- 小红书 / Reddit 为 P0，默认不自动启用，必须在 Agent-Reach + 真实登录态可用后再启用；
- Google News / B站 / YouTube 为 P1；GitHub 为 P2。

注意：点击同步推荐源不会创建任何 evidence、商机、趋势或商品数据。
