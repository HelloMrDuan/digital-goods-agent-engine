# digital-goods-agent-engine

本地自托管的数字商品需求发现与经营辅助系统。系统坚持 `REAL DATA ONLY`：业务数据只能来自真实采集，失败显示真实原因，AI 不得补造搜索量、销量、用户数或趋势。

## V1.2.1 主流程

1. 通过 OpenCLI/Agent-Reach、RSS、GitHub、Bilibili、YouTube 等来源采集真实内容。
2. 明确/高概率需求作为确定性 demand；高质量核心来源 discussion 可作为 `context` 进入商机候选，但不会抬高需求数量、趋势分或商业验证分。
3. TF-IDF 聚类与趋势计算去除镜像重复，并保留原帖、作者、时间、来源和证据关系。
4. 商业验证 Gate 使用真实需求数量、作者/平台多样性、证据质量、时间覆盖和近 30 天需求做确定性判断。
5. 商机发现阶段运行多 Agent：`Signal → Pain → Market → Product → Risk → Judge`，生成 `Opportunity Card` 和 `BUILD / WATCH / DROP` 决策。
6. 商品生成/批准仍要求当前商业验证通过，避免低证据或高风险候选直接进入经营链路。

商业验证不调用 AI，不生成测试业务数据。证据变化后，旧验证自动进入 `STALE`，必须重新验证。多 Agent 运行会记录 provider、model、token、耗时、状态和错误。

批量商机重建默认使用 **3 个商机并发**；单个商机内部六个 Agent 仍保持依赖顺序串行执行。为降低单次 LLM 延迟与 token 消耗，默认 evidence 摘要缩减为 14 条、每条 420 字符，单 Agent 最大输出 800 tokens。可通过 `.env` 的 `AGENT_REBUILD_CONCURRENCY`、`AGENT_DIGEST_SNIPPETS`、`AGENT_SNIPPET_CHARS`、`AGENT_MAX_NEW_TOKENS` 调整。

## 主要 API

- `POST /api/opportunities/rebuild?analyze=true&limit=8`：使用已有真实 evidence 重建候选并批量执行多 Agent；响应会返回当前并发数。
- `GET /api/opportunities/rebuild/tasks/{task_id}`：查看持久化重建进度、并发数和当前 Agent 阶段。
- `GET /api/opportunity-cards`：获取商机卡列表。
- `GET /api/opportunity-cards/{card_id}`：获取商机卡、证据和 Agent 执行记录。
- `GET /api/opportunities/{opportunity_id}/agent-runs`：查看某商机的 Agent 运行明细。

## 启动

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Windows PowerShell 将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。保留既有 `.env` 和 `data/digital_goods.db` 时，启动会自动完成兼容迁移。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app scripts
node --check web/app.js
```

关键配置见 [.env.example](.env.example)，升级步骤见 [UPGRADE_FROM_V1133.md](UPGRADE_FROM_V1133.md)。
