# V1.2.0 Commercial Validation Validation

- 单一真实需求信号无法通过商业验证：PASS
- 两名独立真实需求作者、证据质量与时间覆盖达标时通过：PASS
- 聚类关联证据变化后旧验证自动 `STALE`：PASS
- 验证未通过时，付费 AI provider 不会被调用：PASS
- 验证未通过或已过期时，商品草稿生成被拦截：PASS
- 商品草稿生成后证据变化，人工批准被拦截：PASS
- 商机分析 POST 路由仅注册一次：PASS
- V1.2 单条/查询/批量验证 API 均存在：PASS
- `web/app.js` 已入库并通过 Node 语法检查：PASS
- V1.1.3.3 旧库原地迁移到 V1.2 验证表/字段：PASS
- pytest：47/47 PASS
- Python compileall：PASS
- 发布库不插入任何测试业务数据：PASS

# V1.1.3.3 Windows OpenCLI Command Resolution Validation

- 裸 `opencli` → `shutil.which()` 解析到 `opencli.cmd`：PASS
- 真实 search 路径使用解析后的 Windows npm shim：PASS
- 中文/空格/`&` 等查询参数保持 argv，不经过 shell 拼接：PASS
- V1.1.3.2 UTF-8/GB18030 bytes 解码回归继续保留。
- 手工批量采集可强制绕过已有 BACKOFF，自动任务不受影响：PASS

# V1.1.3.2 Windows CLI Decode Validation

- pytest: 34/34 PASS
- 新增 UTF-8 OpenCLI doctor bytes 回归：PASS
- Agent-Reach JSON + OpenCLI connected 合并：PASS
- Python compileall：PASS
- Frontend JS syntax：PASS
- 发布数据库业务表保持 0：PASS

# V1.1.3.1 Demand Intent Gate + P0 Activation Validation

## 自动化回归

- `pytest`: **29 / 29 PASS**
- Python 全量 `compileall`: PASS
- `node --check web/app.js`: PASS
- 旧 V1.1.2 schema 原地迁移: PASS

## 基于真实平台输出建立的专项测试

本轮测试不是只拿“XX需求”这种人工理想句子，而是覆盖真实采集形态：

- Reddit 根帖 `Need a Budget Spreadsheet – Any Recommendations?` / `I'm looking for...` → `explicit_demand`。
- 评论 `would you mind sharing your spreadsheet?` / `can you PM me a link?` / `can I get a copy?` → `explicit_demand`。
- 评论 `I need something that will auto categorize for me` → demand。
- `I made my own...` / `I've got one...` / `DM me if you need...` → supply/showcase，不计需求。
- 小红书“9000套Excel表格模板 / 模板免费分享 / 我做的财务表”不能因为来源是 P0 就直接算需求。
- 同一句 `Can I get a copy?` 若来自两个真实作者，生成两个独立真实需求信号，不因文本相同被错误去重。

## 小红书评论安全行为

- 评论抓取必须使用搜索结果中的完整签名 URL，包含 `xsec_token`。
- 只给 note id 时不尝试猜 token、不伪造 URL。
- 评论抓取失败时保留已经成功获得的真实搜索 evidence，并记录 enrichment 失败；不会补假评论。

## Reddit 时间处理

OpenCLI `reddit read` 的评论当前不保证提供独立创建时间，因此：

- 父帖：使用真实 `created_utc`；
- 评论缺时间：使用父帖时间作为趋势归属兜底；
- 同时标记 `published_time_quality=inherited_thread`；
- 趋势时间置信度按较低权重计入，防止旧评论在今天重新采集后形成虚假“今日增长”。

## REAL DATA ONLY 验证

旧库迁移后发布数据库业务表仍保持 0 条：

- sources = 0
- evidence = 0
- opportunities = 0
- products = 0

新增 schema 字段存在但不插入任何测试业务数据。

## 环境边界

构建容器没有用户 Windows 的 Chrome 登录态、OpenCLI/Agent-Reach 运行环境，因此容器验收只验证代码、迁移、Gate 和失败行为；不会伪称在构建容器里抓到了小红书/Reddit。

用户 Windows 现场已经能够通过 OpenCLI 获取真实小红书搜索结果和 Reddit 搜索/帖子评论，这是部署侧 P0 数据源验证基础。

## V1.1.3.1 P0 activation

- OpenCLI connected → recommended P0 sources become enabled: PASS
- OpenCLI disconnected → P0 remains disabled: PASS
- Full regression: 31/31 PASS
