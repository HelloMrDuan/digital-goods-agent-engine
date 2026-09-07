# digital-goods-agent-engine

本地自托管的数字商品需求发现与经营辅助系统。系统坚持 `REAL DATA ONLY`：业务数据只能来自真实采集，失败显示真实原因，AI 不得补造搜索量、销量、用户数或趋势。

## V1.2 主流程

1. 通过 OpenCLI/Agent-Reach、RSS、GitHub、Bilibili、YouTube 等来源采集真实内容。
2. 内容级意图 Gate 只保留 `explicit_demand` / `probable_demand` 需求证据。
3. 聚类与趋势计算去除镜像重复，并保留原帖、作者、时间、来源和评论关系。
4. 商业验证 Gate 使用真实需求数量、作者/平台多样性、证据质量、时间覆盖和近 30 天需求做确定性判断。
5. 只有当前证据快照验证通过，才允许调用付费 AI 分析、生成原创商品草稿或批准商品。

商业验证不调用 AI，不生成测试业务数据。证据变化后，旧验证自动进入 `STALE`，必须重新验证。

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
