# V1.2.2 — Gate A / Gate B Product Lifecycle

- 将既有真实需求商业验证显式标准化为 Gate A：`PASS / REJECT / REVIEW_REQUIRED`，继续坚持 evidence-first，不用 AI 补造需求指标。
- 新增确定性 Gate B：检查当前 Gate A、商机分析快照、版权/合规风险、SKU、标题、描述、价格、交付物、FAQ 与合规备注。
- Gate B 结果绑定商品内容 + 商机分析 + Gate A 快照；真实证据或商品内容变化后，旧 Gate B 会自动失效，不能继续批准或交付。
- 商品人工批准收口到 Gate B：只有 `PASS` 才能进入 `APPROVED`，并把对应商机推进到 `READY_TO_PUBLISH`。
- 交付记录扩展 `delivery_type/resource/retry/error/delivered_at`，并要求商品已批准且 Gate B 当前有效，禁止用假资源模拟已交付。
- 新增商品详情、Gate A、Gate B、交付查询 API；前端商机页和商品页直接展示 Gate 状态、阻塞原因和检查项。
- 旧 SQLite 数据库原地迁移，不删除既有真实 evidence、商机、商品或历史交付记录。

# V1.2.0 — Real-Demand Commercial Validation Gate

- 新增零 AI 的商业验证 Gate：基于独立需求信号、明确需求、作者/平台多样性、证据质量、时间覆盖和近 30 天需求判断商机是否可继续投入。
- 验证结果绑定当前真实证据快照；证据新增、聚类变化或已关联证据再次观测后，旧 `PASSED` 自动变为 `STALE`。
- 付费 AI 分析、同步分析任务、商品草稿生成和商品批准统一要求当前验证通过，低质量单点需求不会消耗模型费用。
- 新增单条验证、查询验证、批量验证 API，并在商机详情和工作台展示验证状态与评分。
- 商机分析 API 从重复注册收口为唯一实现，统一复用完整评分、版权风险和商品门槛逻辑。
- Qiniu 主模型与 SenseNova 备用模型通过统一 provider 路由进入相同的证据约束提示和评分流程。
- 补齐此前被页面引用但未入库的 `web/app.js`，恢复工作台、采集、证据、商机、趋势、质量、商品、成本和系统状态页面。
- 旧数据库原地增加验证表和字段，不删除 `.env`、真实 evidence、商机、商品或交付记录。
- 验收：pytest 47/47、Python compileall、前端 `node --check` 全部通过。

# V1.1.3.3 — Windows OpenCLI Command Resolution Hotfix

- 修复 P0 已启用、Doctor 已连接，但真实采集阶段仍报 `opencli: [WinError 2] 系统找不到指定的文件`。
- 根因：Doctor 路径先用 `shutil.which("opencli")` 得到 Windows npm shim（通常为 `opencli.cmd`），而真实 search/read/comments 路径仍把裸 `opencli` 直接交给 `subprocess`，两条链行为不一致。
- 所有 Agent-Reach/OpenCLI CLI 调用现在统一在执行前解析为实际可执行/shim 路径；不改 PATH、不依赖 shell、不拼接用户查询字符串。
- 保留 V1.1.3.2 的 bytes + UTF-8/GB18030 解码修复。
- 新增 Windows `opencli.cmd` 真采集回归，确保 search/read/comments 不再因裸命令触发 WinError 2。
- 页面手工“采集核心需求/采集全部已启用”现在显式 `force=true`，用于代码修复后立即重试；自动调度仍保留退避保护。

# V1.1.3.2 — Windows CLI UTF-8 Decode Hardening

- 修复 Windows 中文系统下 `subprocess(text=True)` 默认按 GBK 解码 OpenCLI / Agent-Reach UTF-8 输出导致的后台线程 `UnicodeDecodeError`。
- `opencli doctor`、`agent-reach doctor --json`、P0 搜索/详情/评论命令统一改为 bytes 捕获后显式解码。
- UTF-8 优先，GB18030 回退，最后 replacement 容错；ASCII 状态标记和 JSON 结构不会再因系统代码页崩溃。
- 直接修复 `/api/sources/activate-core-demand` 因 doctor 误判而返回 409 的问题。
- 新增 Windows CLI 编码回归测试。

# V1.1.3.1 — P0 Login-Aware Activation

- 修复推荐源已同步但 P0 长期显示 DISABLED 的操作断层。
- `同步赚钱优先推荐源` 后会检测真实 OpenCLI Browser Bridge；已连接时自动启用小红书/Reddit P0。
- 新增 `检测登录并启用 P0` 按钮，可在登录态就绪后单独执行。
- 未检测到真实 Browser Bridge / 登录通道时，P0 仍保持关闭，不伪造 READY。
- 启用 != READY：只有实际采集成功后平台状态才会进入 READY。

# V1.1.3 Demand Intent Gate — Real Demand, Not Just Real Content

本轮仍然只沿 V1.1 主线加深：**真实采集 + 真实需求识别 + 跨来源聚类/趋势**。不引入 AgentScope，不扩自动上架。

## 核心问题

V1.1.2 已经保证采到的帖子是真实平台数据，但真实帖子并不等于真实购买/使用需求。例如小红书搜索结果里可能同时出现“求模板”和“9000套模板分享”，Reddit 里也会同时出现求助、供给、推广、普通讨论。

V1.1.3 因此增加 **内容级需求意图 Gate**：

- `explicit_demand`：明确求模板/工具/资源/方案/功能，可进入 P0 商机。
- `probable_demand`：明确痛点或高概率需求，可低权重进入 P0 商机。
- `supply`：提供/销售/分享已有资源，不计需求。
- `showcase`：展示案例、成果、经验，不计需求。
- `discussion`：普通讨论，不计需求。
- `noise`：无关/低信息内容，不计需求。

只有 P0 `core_demand` 来源中被判定为 `explicit_demand` 或 `probable_demand` 的真实 evidence 才能建立需求簇。

## 帖子 + 评论双层采集

Agent-Reach/OpenCLI P0 采集支持 enrich：

- Reddit：搜索后对候选帖子执行 `opencli reddit read <post_id> -f yaml`，把真实评论拆成独立 evidence。
- 小红书：搜索后使用搜索结果返回的**完整签名 URL（含 xsec_token）**执行 `opencli xiaohongshu comments <signed_url> -f yaml`；不能只传 note id。
- 评论保留 `parent_source_item_id / thread_url / content_kind=comment`。
- 同一条帖子重复采集仍只增加 observation；不同真实用户即使说同一句“求一份”，也不会被内容指纹错误合并成一个用户。
- Reddit 评论缺失独立发布时间时，继承父帖时间但标记 `published_time_quality=inherited_thread`，趋势置信度会降权，避免老帖新抓被算成“今天突然爆发”。

## 零 AI 的透明意图规则

意图 Gate 是确定性本地规则，不调用 SenseNova，不烧 API：

- 中文：`求/有没有/哪里下载/能发一份/需要/太麻烦/...`
- 英文：`looking for / need a / any recommendations / can I get / can you share / would you mind sharing / ...`
- 同时识别方向性供给：`我做了 / 免费分享 / I made / I've got one / DM me if you need / I can send you ...`

规则结果会写入证据库：

- `intent_class`
- `intent_confidence`
- `intent_weight`
- `intent_reason_json`

因此每条商机都能反查“为什么被当成需求”。

## V5 聚类与趋势

聚类升级为：

`real_evidence_tfidf_v5_content_intent_gate`

趋势新增：

- `explicit_demand_count`
- `probable_demand_count`
- `comment_demand_count`

并继续保留：独立信号、平台数、作者数、周环比、加速度、趋势置信度等真实统计。

## Agent-Reach/OpenCLI 状态

Doctor 增加 OpenCLI Browser Bridge 探测。即使 Agent-Reach 自身 doctor 尚未把 Reddit/小红书标为 active，只要 `opencli doctor` 显示 Browser Bridge connected，系统会把 OpenCLI 作为可用的真实桌面后端能力进行展示和采集。

## 兼容升级

- 旧 V1.1.2 数据库原地迁移，不要求清库。
- 历史真实 evidence 会执行本地 `backfill_intents()` 补意图字段，不新增或伪造任何 evidence。
- 旧 V4/V3/V2/V1 聚类在重建时退出当前榜单，改由 V5 Gate 重新构建。

## 验收

- pytest: **29 / 29 PASS**
- 旧库迁移: PASS
- 发布业务表保持 0 条测试数据
- Python compileall: PASS
- 前端 `node --check`: PASS
