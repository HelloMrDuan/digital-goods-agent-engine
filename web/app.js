const state = {
  current: "dashboard",
  health: null,
  rebuildTaskId: null,
  apiToken: localStorage.getItem("digitalGoodsApiToken") || "",
};

const views = {
  dashboard: ["工作台", "真实采集、真实证据、真实 Agent 分析。"],
  sources: ["采集中心", "只采集真实平台数据；失败、空结果和退避状态原样展示。"],
  evidence: ["真实证据", "每条需求都可回到原帖、作者、时间和意图判断。"],
  opportunities: ["商机中心", "Evidence → Signal → Pain → Market → Product → Risk → Judge → Opportunity Card。"],
  trends: ["需求趋势", "趋势只由真实 evidence 时间戳和独立信号计算。"],
  market: ["市场情报", "自动采集真实 Marketplace 榜单、互动与明确销量指标；销量口径原样保留。"],
  quality: ["数据质量", "检查来源覆盖、时效性和失败原因。"],
  products: ["商品中心", "仅从通过约束的商机生成原创商品草稿。"],
  cost: ["AI 成本", "展示真实模型调用记录和预算占用。"],
  settings: ["系统状态", "运行模式、采集通道和验证策略。"],
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function n(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : esc(value);
}

function date(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString("zh-CN", { hour12: false });
}

function statusClass(value) {
  const status = String(value || "").toUpperCase();
  if (["READY", "SUCCESS", "PASSED", "APPROVED", "DELIVERED", "ANALYZED", "BUILD"].includes(status)) return "good";
  if (["FAILED", "ERROR", "REJECTED_RISK", "DISABLED", "DROP"].includes(status)) return "bad";
  if (["STALE", "BACKOFF", "EMPTY", "PENDING", "RUNNING", "NOT_VALIDATED", "WATCH", "PARTIAL"].includes(status)) return "warn";
  return "neutral";
}

function pill(value) {
  return `<span class="pill ${statusClass(value)}">${esc(value || "UNKNOWN")}</span>`;
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch (_) {
    return "#";
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(state.apiToken ? { Authorization: `Bearer ${state.apiToken}` } : {}),
      ...(options.headers || {}),
    },
    ...options,
  });
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = body && typeof body === "object" ? body.detail || JSON.stringify(body) : body;
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return body;
}

function showToast(message, bad = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = `toast ${bad ? "bad" : "good"}`;
  setTimeout(() => toast.classList.add("hidden"), 3600);
}

function loading(view) {
  document.getElementById(`view-${view}`).innerHTML = '<div class="panel"><div class="panel-body muted">正在读取真实数据…</div></div>';
}

function empty(message) { return `<div class="empty">${esc(message)}</div>`; }
function stat(label, value, foot = "") { return `<article class="stat-card"><div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${esc(foot)}</div></article>`; }
function panel(title, body, action = "") { return `<section class="panel"><div class="panel-head"><h3>${esc(title)}</h3>${action}</div><div class="panel-body">${body}</div></section>`; }

async function checkHealth() {
  const node = document.getElementById("health-pill");
  try {
    state.health = await api("/health");
    node.textContent = "REAL DATA ONLY";
    node.className = "pill good";
  } catch (_) {
    node.textContent = "服务异常";
    node.className = "pill bad";
  }
}

async function renderDashboard() {
  const data = await api("/api/dashboard");
  const stats = [
    stat("真实证据", data.evidence, `今日 +${data.evidence_today}`),
    stat("候选商机", data.opportunities, `跨来源 ${data.cross_source_opportunities}`),
    stat("验证通过", data.validation_passed || 0, "确定性商业 Gate"),
    stat("市场情报", data.market_intel || 0, `高热 ${data.market_high_heat || 0} / 高销量 ${data.market_high_sales || 0}`),
    stat("商品草稿", data.products, `已批准 ${data.approved}`),
    stat("今日 AI 成本", `¥${n(data.ai_spend_today_cny, 4)}`, `预算 ¥${n(data.ai_budget_today_cny, 2)}`),
  ].join("");
  const opportunities = (data.top_opportunities || []).map((item) => `
    <div class="evidence-card">
      <div class="source-top"><strong>${esc(item.title)}</strong>${pill(item.gate_a?.decision || "NOT_RUN")}</div>
      <div class="metric-row"><span class="metric-chip">信号 ${n(item.effective_signal_count)}</span><span class="metric-chip">明确需求 ${n(item.explicit_demand_count)}</span><span class="metric-chip">平台 ${n(item.platform_count)}</span><span class="metric-chip">可信趋势 ${n(item.trusted_trend_score, 1)}</span></div>
      <button class="btn ghost" onclick="openOpportunity(${Number(item.id)})">查看证据</button>
    </div>`).join("");
  const runs = (data.recent_runs || []).map((run) => `<div class="progress-row"><span>${esc(run.source_name)}</span>${pill(run.status)}<span class="muted">抓取 ${n(run.fetched_count)} / 新增 ${n(run.inserted_count)}</span></div>`).join("");
  document.getElementById("view-dashboard").innerHTML = `<div class="truth-banner"><strong>REAL DATA ONLY</strong><span>${esc(data.truth_policy)}</span></div><div class="grid-stats">${stats}</div><div class="split">${panel("优先商机", opportunities || empty("尚无真实需求候选"))}${panel("最近采集", runs || empty("尚无采集记录"))}</div>`;
}

async function renderSources() {
  const rows = await api("/api/sources");
  const cards = rows.map((source) => `
    <article class="source-card ${esc(source.business_group || "")}">
      <div class="source-top"><div><div class="source-title">${esc(source.name)}</div><div class="source-meta">${esc(source.platform)} · ${esc(source.priority || "—")} · ${esc(source.role || "—")}</div></div>${pill(source.health_status || source.status)}</div>
      <div class="source-query">${esc(source.query || source.url || "未配置查询")}</div>
      ${source.last_error ? `<div class="source-error">${esc(source.last_error)}</div>` : ""}
      <div class="source-foot"><span>上次成功 ${date(source.last_success_at)}</span><button class="btn ghost" onclick="collectSource(${Number(source.id)})">立即采集</button></div>
    </article>`).join("");
  const actions = '<div class="toolbar"><button class="btn secondary" onclick="sourceAction(\'/api/sources/bootstrap\')">同步推荐源</button><button class="btn secondary" onclick="sourceAction(\'/api/sources/activate-core-demand\')">检测登录并启用 P0</button><button class="btn primary" onclick="sourceAction(\'/api/sources/collect-core-demand?force=true\')">采集核心需求</button></div>';
  document.getElementById("view-sources").innerHTML = panel("真实数据源", `<div class="source-card-list">${cards || empty("尚未同步来源")}</div>`, actions);
}

async function renderEvidence() {
  const rows = await api("/api/evidence?limit=200");
  const body = rows.map((item) => `
    <article class="evidence-card">
      <div class="source-top"><strong>${esc(item.title)}</strong>${pill(item.intent_class)}</div>
      <div class="source-meta">${esc(item.platform)} · ${esc(item.author || "作者未知")} · ${date(item.published_at)}</div>
      <p>${esc((item.content || "").slice(0, 300))}</p>
      <div class="metric-row"><span class="metric-chip">质量 ${n(item.evidence_quality_score, 1)}</span><span class="metric-chip">意图 ${n(item.intent_confidence, 2)}</span><span class="metric-chip">${esc(item.content_kind)}</span></div>
      ${item.source_url ? `<a class="evidence-link" href="${esc(safeUrl(item.source_url))}" target="_blank" rel="noreferrer">打开原始证据</a>` : ""}
    </article>`).join("");
  document.getElementById("view-evidence").innerHTML = panel("证据库", body || empty("暂无真实证据"));
}

function priceText(range) {
  if (!range || typeof range !== "object") return "—";
  const currency = range.currency || "CNY";
  const min = range.min ?? "?";
  const max = range.max ?? "?";
  return `${currency} ${min} - ${max}`;
}

function opportunityCardHtml(card) {
  const users = (card.target_users || []).slice(0, 3).map((x) => esc(typeof x === "string" ? x : JSON.stringify(x))).join("、") || "—";
  const pains = (card.pain_points || []).slice(0, 3).map((x) => esc(typeof x === "string" ? x : (x.pain || JSON.stringify(x)))).join("；") || "—";
  const product = card.card?.primary_product || card.recommended_products?.[0]?.name || "—";
  return `<article class="evidence-card">
    <div class="source-top"><div><div class="opportunity-title">${esc(card.title)}</div><div class="source-meta">${esc(card.category || "未分类")} · ${esc((card.source_platforms || []).join(" / ") || "未知来源")}</div></div>${pill(card.decision)}</div>
    <div class="metric-row"><span class="metric-chip">最终分 ${n(card.final_score, 1)}</span><span class="metric-chip">市场 ${n(card.market_score, 1)}</span><span class="metric-chip">风险 ${n(card.risk_score, 1)}</span><span class="metric-chip">购买意愿 ${n(card.purchase_intent, 1)}</span><span class="metric-chip">置信度 ${n(card.confidence, 1)}</span><span class="metric-chip">证据 ${n(card.evidence_count)}</span><span class="metric-chip">Gate A ${esc(card.gate_a?.decision || "NOT_RUN")}</span></div>
    ${card.stale ? '<div class="source-error">证据已变化：该商机卡需要重新分析</div>' : ""}
    <p><strong>目标用户：</strong>${users}</p><p><strong>核心痛点：</strong>${pains}</p><p><strong>优先商品：</strong>${esc(product)} · ${esc(priceText(card.price_range))}</p>
    <div class="toolbar"><button class="btn primary" onclick="openCard(${Number(card.id)})">查看商机卡</button><button class="btn secondary" onclick="runGateA(${Number(card.opportunity_id)})">运行 Gate A</button><button class="btn secondary" onclick="analyzeOpportunity(${Number(card.opportunity_id)})">重新分析</button><button class="btn secondary" onclick="generateProduct(${Number(card.opportunity_id)})">生成商品草稿</button></div>
  </article>`;
}

function candidateHtml(item) {
  return `<article class="evidence-card">
    <div class="source-top"><div><div class="opportunity-title">${esc(item.title)}</div><div class="source-meta">${esc(item.category || "待 Agent 判断")} · ${esc(item.status)}</div></div>${pill(item.validation_status || "NOT_VALIDATED")}</div>
    <div class="metric-row"><span class="metric-chip">独立需求 ${n(item.effective_signal_count)}</span><span class="metric-chip">明确 ${n(item.explicit_demand_count)}</span><span class="metric-chip">平台 ${n(item.platform_count)}</span><span class="metric-chip">质量 ${n(item.data_quality_score, 1)}</span><span class="metric-chip">可信趋势 ${n(item.trusted_trend_score, 1)}</span></div>
    <div class="toolbar"><button class="btn ghost" onclick="openOpportunity(${Number(item.id)})">证据详情</button><button class="btn secondary" onclick="runGateA(${Number(item.id)})">运行 Gate A</button><button class="btn primary" onclick="analyzeOpportunity(${Number(item.id)})">多 Agent 分析</button></div>
  </article>`;
}

async function renderOpportunities() {
  const [cards, rows] = await Promise.all([api("/api/opportunity-cards?limit=200"), api("/api/opportunities?limit=200")]);
  const carded = new Set((cards || []).map((x) => Number(x.opportunity_id)));
  const cardBody = (cards || []).map(opportunityCardHtml).join("");
  const candidateBody = (rows || []).filter((x) => !carded.has(Number(x.id))).map(candidateHtml).join("");
  const actions = '<div class="toolbar"><button class="btn primary" onclick="startRebuild(true)">一键重建 + Agent分析</button><button class="btn secondary" onclick="startRebuild(false)">只重建候选</button><button class="btn secondary" onclick="opportunityAction(\'/api/opportunities/batch/validate\')">批量商业验证</button></div>';
  const progress = '<div id="rebuild-progress" class="muted">未运行批量重建</div>';
  document.getElementById("view-opportunities").innerHTML = `${panel("V1.2 Opportunity Cards", cardBody || empty("暂无商机卡；点击“一键重建 + Agent分析”生成"), actions + progress)}${panel("待分析候选", candidateBody || empty("没有未分析候选"))}`;
  if (state.rebuildTaskId) pollRebuildTask(state.rebuildTaskId, false);
}

async function renderTrends() {
  const rows = await api("/api/trends?limit=100");
  const body = rows.map((item) => `<article class="evidence-card"><div class="source-top"><strong>${esc(item.title)}</strong><span class="score">${n(item.trusted_trend_score, 1)}</span></div><div class="metric-row"><span class="metric-chip">近 7 天 ${n(item.current_7d)}</span><span class="metric-chip">前 7 天 ${n(item.previous_7d)}</span><span class="metric-chip">趋势 ${n(item.trend_score, 1)}</span><span class="metric-chip">置信度 ${n(item.trend_confidence, 1)}</span><span class="metric-chip">平台 ${n(item.platform_count)}</span></div></article>`).join("");
  document.getElementById("view-trends").innerHTML = panel("可信需求趋势", body || empty("暂无趋势数据"));
}

async function renderMarket() {
  const [summary, rows] = await Promise.all([
    api("/api/market-intel/summary"),
    api("/api/market-intel?limit=200"),
  ]);
  const stats = [
    stat("市场商品", summary.total || 0, "真实 Marketplace 观测"),
    stat("高热", summary.high_heat || 0, `阈值 ${n(summary.heat_threshold, 0)}`),
    stat("高销量", summary.high_sales || 0, `阈值 ${n(summary.sales_threshold, 0)}`),
    stat("高热 + 高销量", summary.hot_and_selling || 0, "优先商业情报"),
    stat("有真实销量", summary.verified_sales || 0, "保留 sales_scope"),
  ].join("");
  const cards = (rows || []).map((item) => {
    const sales = item.sales_verified
      ? `${n(item.sales_count)} (${esc(item.sales_scope || "unknown")})`
      : "未公开";
    const badges = [
      item.high_heat ? pill("HIGH_HEAT") : "",
      item.high_sales ? pill("HIGH_SALES") : "",
      item.hot_and_selling ? pill("HOT_SELLING") : "",
    ].join("");
    return `<article class="evidence-card">
      <div class="source-top"><div><strong>${esc(item.title)}</strong><div class="source-meta">${esc(item.marketplace)} · ${esc(item.seller || "seller unknown")}</div></div><div>${badges}</div></div>
      <div class="metric-row"><span class="metric-chip">热度 ${n(item.heat_score,1)}</span><span class="metric-chip">销量分 ${item.sales_score == null ? "—" : n(item.sales_score,1)}</span><span class="metric-chip">销量 ${sales}</span><span class="metric-chip">榜单 #${n(item.rank_position)}</span><span class="metric-chip">市场分 ${n(item.market_score,1)}</span></div>
      <div class="source-meta">评论 ${n(item.review_count)} · 收藏 ${n(item.favorites_count)} · 浏览 ${n(item.views_count)} · 评分 ${n(item.rating,1)}</div>
      ${item.source_url ? `<a class="evidence-link" href="${esc(safeUrl(item.source_url))}" target="_blank" rel="noreferrer">打开市场商品</a>` : ""}
    </article>`;
  }).join("");
  const actions = '<div class="toolbar"><button class="btn primary" onclick="collectMarketIntel()">立即采集市场情报</button><button class="btn secondary" onclick="mutate(\'/api/market-intel/rematch\', \'市场情报已重新匹配商机\')">重新匹配商机</button></div>';
  document.getElementById("view-market").innerHTML = `<div class="grid-stats">${stats}</div>${panel("Market Intelligence", cards || empty("暂无市场情报；点击“立即采集市场情报”"), actions)}`;
}

async function collectMarketIntel() {
  try {
    const result = await api("/api/market-intel/collect?force=true", { method: "POST" });
    showToast(`市场情报采集完成：成功 ${result.success_count || 0} / 失败 ${result.failed_count || 0}`, (result.failed_count || 0) > 0 && (result.success_count || 0) === 0);
    await renderMarket();
  } catch (error) { showToast(error.message, true); }
}

async function renderQuality() {
  const data = await api("/api/data-quality");
  document.getElementById("view-quality").innerHTML = panel("质量报告", `<pre class="code-block">${esc(JSON.stringify(data, null, 2))}</pre>`);
}

async function renderProducts() {
  const rows = await api("/api/products?limit=200");
  const body = rows.map((item) => {
    const gate = item.gate_b || {};
    const gateDecision = gate.is_current === false && gate.decision !== "NOT_RUN" ? "STALE" : (gate.decision || item.gate_status || "NOT_RUN");
    const blockers = (gate.blockers || []).concat(gate.review_flags || []);
    const approve = item.status === "DRAFT" && gateDecision === "PASS"
      ? `<button class="btn primary" onclick="approveProduct(${Number(item.id)})">人工批准</button>`
      : "";
    return `<article class="evidence-card"><div class="source-top"><div><strong>${esc(item.name)}</strong><div class="source-meta mono">${esc(item.sku)}</div></div>${pill(item.status)}</div><p>${esc(item.description || "")}</p><div class="metric-row"><span class="metric-chip">建议价 ¥${n(item.price_suggestion, 2)}</span><span class="metric-chip">交付物 ${(item.deliverables || []).length}</span><span class="metric-chip">Gate B ${esc(gateDecision)}</span><span class="metric-chip">Gate 分 ${n(gate.score ?? item.gate_score, 1)}</span><span class="metric-chip">授权 ${esc(item.license_readiness?.decision || "NOT_CHECKED")}</span></div>${blockers.length ? `<div class="source-error">${esc(blockers.join("、"))}</div>` : ""}<div class="toolbar"><button class="btn secondary" onclick="runGateB(${Number(item.id)})">运行 Gate B</button><button class="btn ghost" onclick="openProduct(${Number(item.id)})">查看详情</button>${approve}</div></article>`;
  }).join("");
  document.getElementById("view-products").innerHTML = panel("商品生命周期 / Gate B", body || empty("暂无商品草稿"));
}

async function renderCost() {
  const data = await api("/api/ai/cost?limit=200");
  const rows = (data.runs || []).map((item) => `<div class="progress-row"><span>${esc(item.operation)}</span>${pill(item.status)}<span>${esc(item.provider)} / ${esc(item.model)}</span><span>¥${n(item.estimated_cost_cny, 6)}</span></div>`).join("");
  document.getElementById("view-cost").innerHTML = `${panel("累计成本", `<div class="stat-value">¥${n(data.total_cost_cny, 6)}</div><div class="muted">每日预算 ¥${n(data.daily_budget_cny, 2)}</div>`)}${panel("调用记录", rows || empty("尚无 AI 调用"))}`;
}

async function renderSettings() {
  const [health, agent, scheduler] = await Promise.all([api("/health"), api("/api/agent-reach/doctor"), api("/api/scheduler/status")]);
  const auth = `<div class="source-meta">远程写操作需要与服务端 API_AUTH_TOKEN 一致的 Token；Token 仅保存在当前浏览器 localStorage。</div>
    <div class="toolbar"><input id="api-token-input" type="password" placeholder="API Token" value="${esc(state.apiToken)}" autocomplete="off"><button class="btn secondary" onclick="saveApiToken()">保存 Token</button><button class="btn ghost" onclick="clearApiToken()">清除</button></div>`;
  document.getElementById("view-settings").innerHTML = `<div class="split">${panel("运行状态", `<pre class="code-block">${esc(JSON.stringify(health, null, 2))}</pre>`)}${panel("采集与调度", `<pre class="code-block">${esc(JSON.stringify({ agent_reach: agent, scheduler }, null, 2))}</pre>`)}</div>${panel("API 写操作认证", auth)}`;
}

function saveApiToken() {
  const input = document.getElementById("api-token-input");
  state.apiToken = String(input?.value || "").trim();
  if (state.apiToken) localStorage.setItem("digitalGoodsApiToken", state.apiToken);
  else localStorage.removeItem("digitalGoodsApiToken");
  showToast(state.apiToken ? "API Token 已保存" : "API Token 已清除");
}

function clearApiToken() {
  state.apiToken = "";
  localStorage.removeItem("digitalGoodsApiToken");
  const input = document.getElementById("api-token-input");
  if (input) input.value = "";
  showToast("API Token 已清除");
}

const renderers = { dashboard: renderDashboard, sources: renderSources, evidence: renderEvidence, opportunities: renderOpportunities, trends: renderTrends, market: renderMarket, quality: renderQuality, products: renderProducts, cost: renderCost, settings: renderSettings };

async function selectView(view) {
  if (!renderers[view]) return;
  state.current = view;
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  document.querySelectorAll(".view").forEach((node) => node.classList.remove("active"));
  document.getElementById(`view-${view}`).classList.add("active");
  document.getElementById("page-title").textContent = views[view][0];
  document.getElementById("page-desc").textContent = views[view][1];
  loading(view);
  try { await renderers[view](); } catch (error) { document.getElementById(`view-${view}`).innerHTML = panel("加载失败", `<div class="source-error">${esc(error.message)}</div>`); }
}

async function refreshCurrent() { await checkHealth(); await selectView(state.current); }

async function mutate(path, success, options = {}) {
  try {
    await api(path, { method: "POST", ...options });
    showToast(success);
    await selectView(state.current);
  } catch (error) { showToast(error.message, true); }
}

async function sourceAction(path) { await mutate(path, "操作完成"); }
async function opportunityAction(path) { await mutate(path, "商机已更新"); }
async function collectSource(id) { await mutate(`/api/sources/${id}/collect?force=true`, "采集完成"); }
async function validateOpportunity(id) { await mutate(`/api/opportunities/${id}/validation`, "商业验证完成"); }
async function runGateA(id) { await mutate(`/api/opportunities/${id}/gate-a`, "Gate A 已更新"); }
async function generateProduct(id) { await mutate(`/api/products/from-opportunity/${id}`, "商品草稿已生成"); }
async function runGateB(id) { await mutate(`/api/products/${id}/gate-b`, "Gate B 已更新"); }
async function approveProduct(id) { await mutate(`/api/products/${id}/approve`, "商品已批准"); }

async function analyzeOpportunity(id) {
  try {
    const started = await api(`/api/opportunities/${id}/analyze/start`, { method: "POST" });
    showToast(`多 Agent 分析已启动，任务 #${started.task_id}`);
    await pollAnalysisTask(started.task_id);
  } catch (error) { showToast(error.message, true); }
}

async function pollAnalysisTask(taskId) {
  for (;;) {
    const task = await api(`/api/analysis/tasks/${taskId}`);
    showToast(`${task.stage || "Agent"} ${n(task.progress)}% · ${task.message || ""}`, task.status === "FAILED");
    if (["SUCCESS", "FAILED"].includes(task.status)) {
      if (state.current === "opportunities") await renderOpportunities();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1400));
  }
}

async function startRebuild(analyze) {
  try {
    const task = await api(`/api/opportunities/rebuild?analyze=${analyze ? "true" : "false"}&limit=8`, { method: "POST" });
    state.rebuildTaskId = task.task_id;
    showToast(`重建任务 #${task.task_id} 已启动`);
    await pollRebuildTask(task.task_id, true);
  } catch (error) { showToast(error.message, true); }
}

async function pollRebuildTask(taskId, loop = true) {
  for (;;) {
    const task = await api(`/api/opportunities/rebuild/tasks/${taskId}`);
    const current = task.current_analysis;
    const stage = current ? `${task.stage} / ${current.stage || "Agent"}` : task.stage;
    const progress = current && task.status === "RUNNING" ? `${task.progress}%（当前商机 ${current.progress}%）` : `${task.progress}%`;
    const node = document.getElementById("rebuild-progress");
    if (node) node.innerHTML = `<strong>${esc(stage || task.status)}</strong> · ${esc(progress)} · ${esc(task.message || "")} · 成功 ${n(task.succeeded)} / 失败 ${n(task.failed)}`;
    if (["SUCCESS", "PARTIAL", "FAILED"].includes(task.status)) {
      showToast(task.message || `任务 ${task.status}`, task.status === "FAILED");
      if (loop && state.current === "opportunities") await renderOpportunities();
      return;
    }
    if (!loop) return;
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}

async function openOpportunity(id) {
  try {
    const item = await api(`/api/opportunities/${id}`);
    document.getElementById("drawer-title").textContent = item.title;
    const validation = item.validation || {};
    const evidence = (item.evidence || []).map((row) => `<article class="evidence-card"><strong>${esc(row.title)}</strong><div class="source-meta">${esc(row.platform)} · ${esc(row.author || "作者未知")} · ${date(row.published_at)}</div>${row.source_url ? `<a class="evidence-link" target="_blank" rel="noreferrer" href="${esc(safeUrl(row.source_url))}">原帖</a>` : ""}</article>`).join("");
    const market = item.market_intel || {};
    const marketRows = (market.items || []).slice(0, 8).map((row) => `<article class="evidence-card"><div class="source-top"><strong>${esc(row.title)}</strong><span class="score">${n(row.market_score,1)}</span></div><div class="metric-row"><span class="metric-chip">热度 ${n(row.heat_score,1)}</span><span class="metric-chip">销量 ${row.sales_verified ? `${n(row.sales_count)} (${esc(row.sales_scope || "unknown")})` : "未公开"}</span><span class="metric-chip">匹配 ${n((row.similarity || 0) * 100,1)}%</span></div></article>`).join("");
    document.getElementById("drawer-body").innerHTML = `<div class="detail-grid"><div class="detail-box"><span>商业验证</span>${pill(validation.status)}</div><div class="detail-box"><span>验证分</span><strong>${n(validation.score, 1)}</strong></div><div class="detail-box"><span>综合分</span><strong>${n(item.overall_score)}</strong></div><div class="detail-box"><span>真实证据</span><strong>${n(item.evidence_count)}</strong></div></div>${validation.reason_codes && validation.reason_codes.length ? `<div class="source-error">${esc(validation.reason_codes.join("、"))}</div>` : ""}<h3 class="section-title">市场情报</h3>${marketRows || empty("暂无匹配市场情报")}<h3 class="section-title">证据</h3>${evidence || empty("无关联证据")}`;
    document.getElementById("drawer").classList.remove("hidden");
  } catch (error) { showToast(error.message, true); }
}

async function openProduct(id) {
  try {
    const item = await api(`/api/products/${id}`);
    const gate = item.gate_b || {};
    const checks = Object.entries(gate.checks || {}).map(([name, check]) => `<div class="progress-row"><span>${esc(name)}</span>${pill(check.ok ? "PASS" : check.severity || "FAIL")}<span>${esc(check.reason || "—")}</span></div>`).join("");
    const license = item.license_readiness || {};
    const assets = (item.assets || []).map((asset) => `<article class="evidence-card"><div class="source-top"><strong>${esc(asset.title)}</strong>${pill(asset.ownership_type || "unknown")}</div><div class="source-meta">${esc(asset.source_type)} · ${esc(asset.role || "source")} · ${esc(asset.license_name || "未登记许可证")}</div><div class="metric-row"><span class="metric-chip">商用 ${asset.commercial_use_allowed === true ? "允许" : asset.commercial_use_allowed === false ? "禁止" : "未知"}</span><span class="metric-chip">再分发 ${asset.redistribution_allowed === true ? "允许" : asset.redistribution_allowed === false ? "禁止" : "未知"}</span></div></article>`).join("");
    document.getElementById("drawer-title").textContent = item.name || item.sku;
    document.getElementById("drawer-body").innerHTML = `<div class="detail-grid"><div class="detail-box"><span>商品状态</span>${pill(item.status)}</div><div class="detail-box"><span>Gate B</span>${pill(gate.is_current === false && gate.decision !== "NOT_RUN" ? "STALE" : (gate.decision || "NOT_RUN"))}</div><div class="detail-box"><span>授权</span>${pill(license.decision || "NOT_CHECKED")}</div><div class="detail-box"><span>建议价</span><strong>¥${n(item.price_suggestion, 2)}</strong></div></div><p><strong>标题：</strong>${esc(item.title || "—")}</p><p><strong>描述：</strong>${esc(item.description || "—")}</p><h3 class="section-title">素材与授权</h3>${assets || empty("尚未绑定 SourceAsset")}<h3 class="section-title">Gate B 检查项</h3>${checks || empty("Gate B 尚未运行")}`;
    document.getElementById("drawer").classList.remove("hidden");
  } catch (error) { showToast(error.message, true); }
}

async function openCard(cardId) {
  try {
    const card = await api(`/api/opportunity-cards/${cardId}`);
    document.getElementById("drawer-title").textContent = card.title;
    const pains = (card.pain_points || []).map((x) => `<li>${esc(typeof x === "string" ? x : (x.pain || JSON.stringify(x)))}</li>`).join("");
    const products = (card.recommended_products || []).map((x) => `<li><strong>${esc(x.name || "未命名商品")}</strong> · ${esc(x.format || "")}</li>`).join("");
    const warnings = (card.warnings || []).map((x) => `<li>${esc(typeof x === "string" ? x : JSON.stringify(x))}</li>`).join("");
    const runs = (card.agent_runs || []).slice().reverse().map((x) => `<div class="progress-row"><span>${esc(x.agent_name)}</span>${pill(x.status)}<span>${esc(x.provider || "—")} / ${esc(x.model || "—")}</span><span>${n(x.duration_ms)}ms · ${n(x.total_tokens)} tokens</span></div>`).join("");
    document.getElementById("drawer-body").innerHTML = `<div class="detail-grid"><div class="detail-box"><span>决策</span>${pill(card.decision)}</div><div class="detail-box"><span>最终分</span><strong>${n(card.final_score, 1)}</strong></div><div class="detail-box"><span>市场分</span><strong>${n(card.market_score, 1)}</strong></div><div class="detail-box"><span>风险分</span><strong>${n(card.risk_score, 1)}</strong></div></div><p><strong>目标用户：</strong>${esc((card.target_users || []).join("、") || "—")}</p><p><strong>价格：</strong>${esc(priceText(card.price_range))}</p><h3 class="section-title">痛点</h3><ul>${pains || "<li>—</li>"}</ul><h3 class="section-title">推荐商品</h3><ul>${products || "<li>—</li>"}</ul><h3 class="section-title">风险提示</h3><ul>${warnings || "<li>—</li>"}</ul><h3 class="section-title">Judge 结论</h3><p>${esc(card.reasoning || "—")}</p><h3 class="section-title">Agent 执行记录</h3>${runs || empty("暂无 Agent 记录")}`;
    document.getElementById("drawer").classList.remove("hidden");
  } catch (error) { showToast(error.message, true); }
}

function closeDrawer(event) {
  if (event && event.target !== event.currentTarget) return;
  document.getElementById("drawer").classList.add("hidden");
}

document.querySelectorAll(".nav-item").forEach((node) => node.addEventListener("click", () => selectView(node.dataset.view)));
window.refreshCurrent = refreshCurrent;
window.closeDrawer = closeDrawer;
window.collectSource = collectSource;
window.sourceAction = sourceAction;
window.opportunityAction = opportunityAction;
window.validateOpportunity = validateOpportunity;
window.runGateA = runGateA;
window.analyzeOpportunity = analyzeOpportunity;
window.generateProduct = generateProduct;
window.runGateB = runGateB;
window.approveProduct = approveProduct;
window.openOpportunity = openOpportunity;
window.openProduct = openProduct;
window.openCard = openCard;
window.startRebuild = startRebuild;

checkHealth();
selectView("dashboard");
