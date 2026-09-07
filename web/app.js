const state = { current: "dashboard", health: null };

const views = {
  dashboard: ["工作台", "真实采集、真实证据、商业验证后才调用 AI。"],
  sources: ["采集中心", "只采集真实平台数据；失败、空结果和退避状态原样展示。"],
  evidence: ["真实证据", "每条需求都可回到原帖、作者、时间和意图判断。"],
  opportunities: ["商机中心", "先通过 V1.2 商业验证 Gate，再进行付费 AI 分析。"],
  trends: ["需求趋势", "趋势只由真实 evidence 时间戳和独立信号计算。"],
  quality: ["数据质量", "检查来源覆盖、时效性和失败原因。"],
  products: ["商品中心", "仅从已验证、已分析、低风险商机生成原创商品草稿。"],
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
  if (["READY", "SUCCESS", "PASSED", "APPROVED", "DELIVERED", "ANALYZED"].includes(status)) return "good";
  if (["FAILED", "ERROR", "REJECTED_RISK", "DISABLED"].includes(status)) return "bad";
  if (["STALE", "BACKOFF", "EMPTY", "PENDING", "RUNNING", "NOT_VALIDATED"].includes(status)) return "warn";
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
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
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
  setTimeout(() => toast.classList.add("hidden"), 3200);
}

function loading(view) {
  document.getElementById(`view-${view}`).innerHTML = '<div class="panel"><div class="panel-body muted">正在读取真实数据…</div></div>';
}

function empty(message) {
  return `<div class="empty">${esc(message)}</div>`;
}

function stat(label, value, foot = "") {
  return `<article class="stat-card"><div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${esc(foot)}</div></article>`;
}

function panel(title, body, action = "") {
  return `<section class="panel"><div class="panel-head"><h3>${esc(title)}</h3>${action}</div><div class="panel-body">${body}</div></section>`;
}

async function checkHealth() {
  const node = document.getElementById("health-pill");
  try {
    state.health = await api("/health");
    node.textContent = "REAL DATA ONLY";
    node.className = "pill good";
  } catch (error) {
    node.textContent = "服务异常";
    node.className = "pill bad";
  }
}

async function renderDashboard() {
  const data = await api("/api/dashboard");
  const stats = [
    stat("真实证据", data.evidence, `今日 +${data.evidence_today}`),
    stat("有效商机", data.opportunities, `跨来源 ${data.cross_source_opportunities}`),
    stat("验证通过", data.validation_passed || 0, "V1.2 商业 Gate"),
    stat("商品草稿", data.products, `已批准 ${data.approved}`),
    stat("今日 AI 成本", `¥${n(data.ai_spend_today_cny, 4)}`, `预算 ¥${n(data.ai_budget_today_cny, 2)}`),
  ].join("");
  const opportunities = (data.top_opportunities || []).map((item) => `
    <div class="evidence-card">
      <div class="source-top"><strong>${esc(item.title)}</strong>${pill(item.validation_status || "NOT_VALIDATED")}</div>
      <div class="metric-row">
        <span class="metric-chip">信号 ${n(item.effective_signal_count)}</span>
        <span class="metric-chip">明确需求 ${n(item.explicit_demand_count)}</span>
        <span class="metric-chip">平台 ${n(item.platform_count)}</span>
        <span class="metric-chip">可信趋势 ${n(item.trusted_trend_score, 1)}</span>
      </div>
      <button class="btn ghost" onclick="openOpportunity(${Number(item.id)})">查看证据</button>
    </div>`).join("");
  const runs = (data.recent_runs || []).map((run) => `
    <div class="progress-row"><span>${esc(run.source_name)}</span>${pill(run.status)}<span class="muted">抓取 ${n(run.fetched_count)} / 新增 ${n(run.inserted_count)}</span></div>`).join("");
  document.getElementById("view-dashboard").innerHTML = `
    <div class="truth-banner"><strong>REAL DATA ONLY</strong><span>${esc(data.truth_policy)}</span></div>
    <div class="grid-stats">${stats}</div>
    <div class="split">${panel("优先商机", opportunities || empty("尚无通过意图 Gate 的真实需求"))}${panel("最近采集", runs || empty("尚无采集记录"))}</div>`;
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

async function renderOpportunities() {
  const rows = await api("/api/opportunities?limit=200");
  const body = rows.map((item) => `
    <article class="evidence-card">
      <div class="source-top"><div><div class="opportunity-title">${esc(item.title)}</div><div class="source-meta">${esc(item.category || "待分析")} · ${esc(item.status)}</div></div>${pill(item.validation_status || "NOT_VALIDATED")}</div>
      <div class="metric-row">
        <span class="metric-chip">独立需求 ${n(item.effective_signal_count)}</span>
        <span class="metric-chip">明确 ${n(item.explicit_demand_count)}</span>
        <span class="metric-chip">平台 ${n(item.platform_count)}</span>
        <span class="metric-chip">质量 ${n(item.data_quality_score, 1)}</span>
        <span class="metric-chip">验证分 ${n(item.validation_score, 1)}</span>
        <span class="metric-chip">综合分 ${n(item.overall_score)}</span>
      </div>
      <div class="toolbar"><button class="btn ghost" onclick="openOpportunity(${Number(item.id)})">详情</button><button class="btn secondary" onclick="validateOpportunity(${Number(item.id)})">商业验证</button><button class="btn primary" onclick="analyzeOpportunity(${Number(item.id)})">AI 分析</button><button class="btn secondary" onclick="generateProduct(${Number(item.id)})">生成商品草稿</button></div>
    </article>`).join("");
  const actions = '<div class="toolbar"><button class="btn secondary" onclick="opportunityAction(\'/api/opportunities/build\')">重建商机</button><button class="btn primary" onclick="opportunityAction(\'/api/opportunities/batch/validate\')">批量商业验证</button></div>';
  document.getElementById("view-opportunities").innerHTML = panel("真实需求商机", body || empty("暂无商机；先在采集中心获取真实需求"), actions);
}

async function renderTrends() {
  const rows = await api("/api/trends?limit=100");
  const body = rows.map((item) => `
    <article class="evidence-card"><div class="source-top"><strong>${esc(item.title)}</strong><span class="score">${n(item.trusted_trend_score, 1)}</span></div>
      <div class="metric-row"><span class="metric-chip">近 7 天 ${n(item.current_7d)}</span><span class="metric-chip">前 7 天 ${n(item.previous_7d)}</span><span class="metric-chip">趋势 ${n(item.trend_score, 1)}</span><span class="metric-chip">置信度 ${n(item.trend_confidence, 1)}</span><span class="metric-chip">平台 ${n(item.platform_count)}</span></div>
    </article>`).join("");
  document.getElementById("view-trends").innerHTML = panel("可信需求趋势", body || empty("暂无趋势数据"));
}

async function renderQuality() {
  const data = await api("/api/data-quality");
  document.getElementById("view-quality").innerHTML = panel("质量报告", `<pre class="code-block">${esc(JSON.stringify(data, null, 2))}</pre>`);
}

async function renderProducts() {
  const rows = await api("/api/products?limit=200");
  const body = rows.map((item) => `
    <article class="evidence-card"><div class="source-top"><div><strong>${esc(item.name)}</strong><div class="source-meta mono">${esc(item.sku)}</div></div>${pill(item.status)}</div>
      <p>${esc(item.description || "")}</p><div class="metric-row"><span class="metric-chip">建议价 ¥${n(item.price_suggestion, 2)}</span><span class="metric-chip">交付物 ${(item.deliverables || []).length}</span></div>
      ${item.status === "DRAFT" ? `<button class="btn primary" onclick="approveProduct(${Number(item.id)})">人工批准</button>` : ""}
    </article>`).join("");
  document.getElementById("view-products").innerHTML = panel("商品草稿", body || empty("暂无商品草稿"));
}

async function renderCost() {
  const data = await api("/api/ai/cost?limit=200");
  const rows = (data.runs || []).map((item) => `<div class="progress-row"><span>${esc(item.operation)}</span>${pill(item.status)}<span>${esc(item.provider)} / ${esc(item.model)}</span><span>¥${n(item.estimated_cost_cny, 6)}</span></div>`).join("");
  document.getElementById("view-cost").innerHTML = `${panel("累计成本", `<div class="stat-value">¥${n(data.total_cost_cny, 6)}</div><div class="muted">每日预算 ¥${n(data.daily_budget_cny, 2)}</div>`)}${panel("调用记录", rows || empty("尚无 AI 调用"))}`;
}

async function renderSettings() {
  const [health, agent, scheduler] = await Promise.all([api("/health"), api("/api/agent-reach/doctor"), api("/api/scheduler/status")]);
  document.getElementById("view-settings").innerHTML = `<div class="split">${panel("运行状态", `<pre class="code-block">${esc(JSON.stringify(health, null, 2))}</pre>`)}${panel("采集与调度", `<pre class="code-block">${esc(JSON.stringify({ agent_reach: agent, scheduler }, null, 2))}</pre>`)}</div>`;
}

const renderers = { dashboard: renderDashboard, sources: renderSources, evidence: renderEvidence, opportunities: renderOpportunities, trends: renderTrends, quality: renderQuality, products: renderProducts, cost: renderCost, settings: renderSettings };

async function selectView(view) {
  if (!renderers[view]) return;
  state.current = view;
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  document.querySelectorAll(".view").forEach((node) => node.classList.remove("active"));
  document.getElementById(`view-${view}`).classList.add("active");
  document.getElementById("page-title").textContent = views[view][0];
  document.getElementById("page-desc").textContent = views[view][1];
  loading(view);
  try {
    await renderers[view]();
  } catch (error) {
    document.getElementById(`view-${view}`).innerHTML = panel("加载失败", `<div class="source-error">${esc(error.message)}</div>`);
  }
}

async function refreshCurrent() { await checkHealth(); await selectView(state.current); }

async function mutate(path, success, options = {}) {
  try {
    await api(path, { method: "POST", ...options });
    showToast(success);
    await selectView(state.current);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function sourceAction(path) { await mutate(path, "操作完成"); }
async function opportunityAction(path) { await mutate(path, "商机已更新"); }
async function collectSource(id) { await mutate(`/api/sources/${id}/collect?force=true`, "采集完成"); }
async function validateOpportunity(id) { await mutate(`/api/opportunities/${id}/validation`, "商业验证完成"); }
async function analyzeOpportunity(id) { await mutate(`/api/opportunities/${id}/analyze`, "AI 分析完成"); }
async function generateProduct(id) { await mutate(`/api/products/from-opportunity/${id}`, "商品草稿已生成"); }
async function approveProduct(id) { await mutate(`/api/products/${id}/approve`, "商品已批准"); }

async function openOpportunity(id) {
  try {
    const item = await api(`/api/opportunities/${id}`);
    document.getElementById("drawer-title").textContent = item.title;
    const validation = item.validation || {};
    const evidence = (item.evidence || []).map((row) => `<article class="evidence-card"><strong>${esc(row.title)}</strong><div class="source-meta">${esc(row.platform)} · ${esc(row.author || "作者未知")} · ${date(row.published_at)}</div>${row.source_url ? `<a class="evidence-link" target="_blank" rel="noreferrer" href="${esc(safeUrl(row.source_url))}">原帖</a>` : ""}</article>`).join("");
    document.getElementById("drawer-body").innerHTML = `
      <div class="detail-grid"><div class="detail-box"><span>商业验证</span>${pill(validation.status)}</div><div class="detail-box"><span>验证分</span><strong>${n(validation.score, 1)}</strong></div><div class="detail-box"><span>综合分</span><strong>${n(item.overall_score)}</strong></div><div class="detail-box"><span>真实证据</span><strong>${n(item.evidence_count)}</strong></div></div>
      ${validation.reason_codes && validation.reason_codes.length ? `<div class="source-error">${esc(validation.reason_codes.join("、"))}</div>` : ""}
      <h3 class="section-title">证据</h3>${evidence || empty("无关联证据")}`;
    document.getElementById("drawer").classList.remove("hidden");
  } catch (error) {
    showToast(error.message, true);
  }
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
window.analyzeOpportunity = analyzeOpportunity;
window.generateProduct = generateProduct;
window.approveProduct = approveProduct;
window.openOpportunity = openOpportunity;

checkHealth();
selectView("dashboard");
