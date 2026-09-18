# digital-goods-agent-engine

本地自托管的数字商品需求发现与经营辅助系统。系统坚持 `REAL DATA ONLY`：业务数据只能来自真实采集，失败显示真实原因，AI 不得补造搜索量、销量、用户数或趋势。

## V1.2.3 主流程

1. 通过 OpenCLI/Agent-Reach、RSS、GitHub、Bilibili、YouTube 等来源采集真实内容。
2. 明确/高概率需求作为确定性 demand；高质量核心来源 discussion 可作为 `context` 进入商机候选，但不会抬高需求数量、趋势分或商业验证分。
3. TF-IDF 聚类与趋势计算去除镜像重复，并保留原帖、作者、时间、来源和证据关系。
4. 商业验证 Gate 使用真实需求数量、作者/平台多样性、证据质量、时间覆盖和近 30 天需求做确定性判断。
5. 商机发现阶段运行多 Agent：`Signal → Pain → Market → Product → Risk → Judge`，生成 `Opportunity Card` 和 `BUILD / WATCH / DROP` 决策。
6. Gate A 将商业验证标准化为 `PASS / REJECT / REVIEW_REQUIRED`，只有当前真实需求快照满足条件才允许进入商品生成。
7. Gate B 对商品标题、描述、价格、交付物、客服 FAQ、合规备注、版权风险和上游 Gate A 快照做确定性检查，输出 `PASS / BLOCKED / REVIEW_REQUIRED`。
8. 商品草稿可以进一步执行真实交付包构建：系统根据真实需求证据生成 README、文本/脚本/CSV，并可生成 XLSX，打包为 ZIP；生成包登记为 self-owned SourceAsset。
9. 人工批准只能在当前 Gate B 为 `PASS` 时执行；批准后商品进入 `APPROVED`，商机进入 `READY_TO_PUBLISH`。
10. 配置 Etsy Open API v3 后，可把已批准且 Gate B 当前有效的数字商品创建为 Etsy draft listing、上传 ZIP 数字文件、上传 listing image，并激活为 active listing。未配置凭证时明确失败，不生成“已发布”假状态。
11. 交付记录要求商品已批准且 Gate B 仍然有效；上游证据变化会让旧 Gate B 失效，阻止继续自动交付。
12. SourceAsset Registry 独立记录素材来源、所有权、许可证、商用许可、再分发许可和证据；Gate B 只使用显式授权事实，不根据 URL、仓库名或“开源”字样猜测可商用。

Gate A / Gate B 都是确定性 Gate，不调用 AI、不生成测试业务数据。证据变化后，旧 Gate A 自动进入 `STALE`；Gate B 绑定商品内容、商机分析和 Gate A 快照，上游任一变化都会变为非当前状态，必须重新检查。多 Agent 运行仍记录 provider、model、token、耗时、状态和错误。

批量商机重建默认使用 **3 个商机并发**；单个商机内部六个 Agent 仍保持依赖顺序串行执行。为降低单次 LLM 延迟与 token 消耗，默认 evidence 摘要缩减为 14 条、每条 420 字符，单 Agent 最大输出 800 tokens。可通过 `.env` 的 `AGENT_REBUILD_CONCURRENCY`、`AGENT_DIGEST_SNIPPETS`、`AGENT_SNIPPET_CHARS`、`AGENT_MAX_NEW_TOKENS` 调整。

## 主要 API

- `POST /api/opportunities/rebuild?analyze=true&limit=8`：使用已有真实 evidence 重建候选并批量执行多 Agent；响应会返回当前并发数。
- `GET /api/opportunities/rebuild/tasks/{task_id}`：查看持久化重建进度、并发数和当前 Agent 阶段。
- `GET /api/opportunity-cards`：获取商机卡列表。
- `GET /api/opportunity-cards/{card_id}`：获取商机卡、证据和 Agent 执行记录。
- `GET /api/opportunities/{opportunity_id}/agent-runs`：查看某商机的 Agent 运行明细。
- `GET/POST /api/opportunities/{opportunity_id}/gate-a`：查询或重新执行 Gate A。
- `GET /api/products/{product_id}`：查看商品详情及当前 Gate B。
- `GET/POST /api/products/{product_id}/gate-b`：查询或重新执行商品上线前 Gate B。
- `POST /api/products/{product_id}/build-artifact`：生成真实可下载商品 ZIP，并登记自有 SourceAsset。
- `GET /api/products/{product_id}/artifact/download`：下载最新成功构建的商品包。
- `POST /api/products/{product_id}/approve`：人工批准；仅当前 Gate B 为 `PASS` 时允许。
- `GET /api/publish/etsy/status`：检查 Etsy 真发布配置是否完整。
- `POST /api/products/{product_id}/publish/etsy`：调用 Etsy Open API v3 真正创建数字商品 listing、上传文件/图片并激活。
- `GET/POST /api/deliveries`：查询/创建交付记录；创建交付要求商品已批准且 Gate B 仍然有效。
- `GET/POST /api/source-assets`：查询/登记来源素材和授权元数据。
- `GET/PATCH /api/source-assets/{asset_id}`：查看或修正素材所有权/许可证证据。
- `GET /api/products/{product_id}/assets`：查看商品绑定素材及聚合授权状态。
- `POST/DELETE /api/products/{product_id}/assets/{asset_id}`：绑定或解绑商品素材。
- `GET /api/products/{product_id}/license-readiness`：获取明确的商用/再分发授权 Gate。

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
