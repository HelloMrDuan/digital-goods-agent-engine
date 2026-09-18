from __future__ import annotations

import json
import threading
import httpx
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .collectors import agent_reach_doctor, rsshub_doctor
from .config import BASE_DIR, settings
from .db import conn, init_db, loads, now_iso, row_to_dict
from .models import DeliveryIn, SourceAssetCreate, SourceAssetUpdate, SourceCreate, SourceUpdate
from .services.analyze import analyze_one, product_from_opportunity
from .services.opportunities import build_from_unlinked_evidence, get_evidence_for_opportunity, get_opportunity, list_opportunities
from .services.clustering import latest_cluster_run, trend_detail, trend_ranking
from .services.sources import bootstrap_sources, collect_source, create_source, get_source, list_sources, platform_coverage
from .services.scheduler import run_cycle as scheduler_run_cycle, scheduler_status, start_scheduler, stop_scheduler
from .services.quality import data_quality_report
from .services.ingest import backfill_intents
from .services.validation import get_gate_a, get_validation, require_current_validation, validate_active_opportunities, validate_opportunity
from .services.product_lifecycle import approve_product, evaluate_gate_b, get_gate_b, get_product, list_delivery_records, require_approved_current_product
from .services.asset_registry import create_source_asset, get_source_asset, link_product_asset, list_product_assets, list_source_assets, product_asset_readiness, unlink_product_asset, update_source_asset
from .services.market_intel import list_market_intel, market_intel_for_opportunity, market_intel_summary, rematch_market_intel
from .services.product_builder import build_product_artifact, latest_successful_bundle, list_product_builds
from .services.publishing import etsy_status, list_publish_runs, publish_to_etsy

WEB_DIR = BASE_DIR / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    bootstrap_sources()
    backfill_intents()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title=settings.app_name, version="1.2.0-commercial-validation", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def home():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": "REAL_DATA_ONLY",
        "mock_data": False,
        "mock_ai": False,
        "llm_configured": settings.is_llm_configured,
        "qiniu_configured": settings.is_qiniu_configured,
        "qiniu_model": settings.qiniu_model if settings.is_qiniu_configured else None,
        "sensenova_configured": settings.is_sensenova_configured,
        "sensenova_model": settings.sensenova_model or None,
        "primary_llm_provider": "Qiniu" if settings.is_qiniu_configured else "SenseNova" if settings.is_sensenova_configured else None,
        "daily_ai_budget_cny": settings.daily_ai_budget_cny,
        "rsshub_configured": bool(settings.rsshub_base_url),
        "cluster_method": "real_evidence_tfidf_v5_content_intent_gate",
        "trend_support_similarity_threshold": settings.trend_support_similarity_threshold,
        "source_stale_after_hours": settings.source_stale_after_hours,
        "cluster_window_days": settings.cluster_window_days,
        "cluster_similarity_threshold": settings.cluster_similarity_threshold,
        "auto_collect_enabled": settings.auto_collect_enabled,
        "auto_collect_interval_minutes": settings.auto_collect_interval_minutes,
        "commercial_validation_policy": "commercial-validation-v1-real-demand",
        "market_intel_enabled": True,
        "market_intel_high_heat_score": settings.market_intel_high_heat_score,
        "market_intel_high_sales_score": settings.market_intel_high_sales_score,
        "artifact_generation_enabled": True,
        "etsy_publishing": etsy_status(),
        "db": settings.db_path,
    }


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    today = now_iso()[:10]
    with conn() as c:
        counts = {
            "sources": c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"],
            "sources_ready": c.execute("SELECT COUNT(*) c FROM sources WHERE status='READY'").fetchone()["c"],
            "evidence": c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"],
            "evidence_today": c.execute("SELECT COUNT(*) c FROM evidence WHERE substr(collected_at,1,10)=?", (today,)).fetchone()["c"],
            "opportunities": c.execute("SELECT COUNT(*) c FROM opportunities WHERE cluster_active=1").fetchone()["c"],
            "analyzed": c.execute("SELECT COUNT(*) c FROM opportunities WHERE cluster_active=1 AND status IN ('ANALYZED','PRODUCT_DRAFTED')").fetchone()["c"],
            "cross_source_opportunities": c.execute("SELECT COUNT(*) c FROM opportunities WHERE cluster_active=1 AND platform_count>=2").fetchone()["c"],
            "products": c.execute("SELECT COUNT(*) c FROM products").fetchone()["c"],
            "approved": c.execute("SELECT COUNT(*) c FROM products WHERE status='APPROVED'").fetchone()["c"],
            "validation_passed": c.execute("SELECT COUNT(*) c FROM opportunities WHERE cluster_active=1 AND validation_status='PASSED'").fetchone()["c"],
            "market_intel": c.execute("SELECT COUNT(*) c FROM market_intel_items").fetchone()["c"],
            "market_high_heat": c.execute("SELECT COUNT(*) c FROM market_intel_items WHERE heat_score>=?", (settings.market_intel_high_heat_score,)).fetchone()["c"],
            "market_high_sales": c.execute("SELECT COUNT(*) c FROM market_intel_items WHERE sales_verified=1 AND sales_score>=?", (settings.market_intel_high_sales_score,)).fetchone()["c"],
            "market_hot_and_selling": c.execute("SELECT COUNT(*) c FROM market_intel_items WHERE heat_score>=? AND sales_verified=1 AND sales_score>=?", (settings.market_intel_high_heat_score, settings.market_intel_high_sales_score)).fetchone()["c"],
        }
        spend = c.execute("SELECT coalesce(sum(estimated_cost_cny),0) s FROM ai_runs WHERE status='SUCCESS' AND substr(created_at,1,10)=?", (today,)).fetchone()["s"]
        top = c.execute("SELECT id,title,category,evidence_count,effective_signal_count,explicit_demand_count,probable_demand_count,comment_demand_count,platform_count,current_7d,previous_7d,growth_7d_pct,acceleration_pct,trend_score,trend_confidence,trusted_trend_score,data_quality_score,validation_status,validation_score,overall_score,suggested_product,status FROM opportunities WHERE cluster_active=1 ORDER BY coalesce(trusted_trend_score,0) DESC,coalesce(trend_score,0) DESC,coalesce(overall_score,-1) DESC LIMIT 8").fetchall()
        source_health = c.execute("SELECT id,name,platform,status,last_item_count,last_success_at,last_error,failure_streak,empty_streak,next_retry_at,avg_duration_ms FROM sources ORDER BY status DESC,id LIMIT 12").fetchall()
        recent_runs = c.execute("SELECT cr.*,s.name source_name,s.platform FROM collection_runs cr JOIN sources s ON s.id=cr.source_id ORDER BY cr.id DESC LIMIT 10").fetchall()
    return {
        **counts,
        "ai_spend_today_cny": round(float(spend or 0), 6),
        "ai_budget_today_cny": settings.daily_ai_budget_cny,
        "top_opportunities": [row_to_dict(r) for r in top],
        "source_health": [row_to_dict(r) for r in source_health],
        "recent_runs": [row_to_dict(r) for r in recent_runs],
        "latest_cluster_run": latest_cluster_run(),
        "market_intel_summary": market_intel_summary(),
        "truth_policy": "所有业务数据来自真实采集；无数据即为0，失败显示失败原因。趋势指标只由真实 evidence 时间戳计算；市场销量只有来源明确暴露时才记录，并保留 listing/shop/account 口径。",
    }




def _run_analysis_task(task_id: int, opportunity_id: int):
    try:
        with conn() as c:
            c.execute("UPDATE analysis_tasks SET status='RUNNING',progress=10,stage='读取商机',message='正在读取真实证据',updated_at=? WHERE id=?", (now_iso(), task_id))
        with conn() as c:
            c.execute("UPDATE analysis_tasks SET progress=35,stage='整理证据',message='正在汇总需求证据',updated_at=? WHERE id=?", (now_iso(), task_id))
        with conn() as c:
            c.execute("UPDATE analysis_tasks SET progress=55,stage='AI分析',message='正在调用 Qiniu qwen/qwen3.5-plus',updated_at=? WHERE id=?", (now_iso(), task_id))
        result = analyze_one(opportunity_id)
        with conn() as c:
            c.execute("UPDATE analysis_tasks SET status='SUCCESS',progress=100,stage='完成',message='分析完成',result_json=?,updated_at=? WHERE id=?", (dumps(result), now_iso(), task_id))
    except Exception as exc:
        with conn() as c:
            c.execute("UPDATE analysis_tasks SET status='FAILED',progress=100,message=?,error=?,updated_at=? WHERE id=?", (str(exc), str(exc), now_iso(), task_id))


@app.post("/api/opportunities/{opportunity_id}/analyze/start")
def opportunities_analyze_start(opportunity_id: int):
    if not get_opportunity(opportunity_id):
        raise HTTPException(404, "opportunity not found")

    # Discovery analysis intentionally accepts real-evidence candidates that have not
    # passed deterministic commercial validation; the decision guard can only emit
    # WATCH/DROP until validation passes. Product generation remains Gate-A protected.
    now = now_iso()
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """UPDATE analysis_tasks
               SET status='FAILED',progress=100,stage='失败',message='stale analysis task expired',
                   error='stale analysis task expired',updated_at=?
               WHERE opportunity_id=? AND status IN ('PENDING','RUNNING') AND updated_at<?""",
            (now, opportunity_id, stale_cutoff),
        )
        active = c.execute(
            """SELECT id,status,progress,stage FROM analysis_tasks
               WHERE opportunity_id=? AND status IN ('PENDING','RUNNING')
               ORDER BY id DESC LIMIT 1""",
            (opportunity_id,),
        ).fetchone()
        if active:
            raise HTTPException(
                409,
                detail={
                    "message": "opportunity analysis already running",
                    "task_id": int(active["id"]),
                    "status": active["status"],
                    "progress": int(active["progress"] or 0),
                    "stage": active["stage"],
                },
            )
        cur = c.execute(
            "INSERT INTO analysis_tasks(opportunity_id,status,progress,stage,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (opportunity_id, "PENDING", 0, "排队", "等待分析", now, now),
        )
        task_id = int(cur.lastrowid)
    threading.Thread(target=_run_analysis_task, args=(task_id, opportunity_id), daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/analysis/tasks/{task_id}")
def analysis_task(task_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM analysis_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    return row_to_dict(row)


@app.get("/api/opportunities/{opportunity_id}/analysis")
def opportunities_analysis(opportunity_id: int):
    with conn() as c:
        row = c.execute("SELECT analysis_json FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    if not row:
        raise HTTPException(404, "opportunity not found")
    return json.loads(row["analysis_json"]) if row["analysis_json"] else {}

@app.post("/api/sources/bootstrap")
def sources_bootstrap():
    result = bootstrap_sources()
    # Sync source definitions first, then opportunistically activate P0 only when
    # the real OpenCLI Browser Bridge is already connected. Never fake readiness.
    try:
        activation = _activate_p0_from_real_login()
    except Exception as exc:
        activation = {"activated": 0, "eligible": 0, "ready": False, "reason": str(exc)}
    result["p0_activation"] = activation
    return result


def _activate_p0_from_real_login() -> dict[str, Any]:
    status = agent_reach_doctor()
    opencli = status.get("opencli") if isinstance(status, dict) else None
    via_bridge = bool(status.get("via_bridge")) if isinstance(status, dict) else False
    connected = bool(isinstance(opencli, dict) and opencli.get("connected"))

    # Bridge mode may not expose opencli details; require effective channel readiness instead.
    channels = status.get("channels") if isinstance(status, dict) else {}
    def channel_ready(name: str) -> bool:
        info = channels.get(name) if isinstance(channels, dict) else None
        if not isinstance(info, dict):
            return False
        value = str(info.get("effective_status") or info.get("status") or "").lower()
        backend = str(info.get("effective_backend") or info.get("active_backend") or "").lower()
        return value in {"ok", "ready", "connected"} or "opencli" in backend

    xhs_ready = connected or (via_bridge and channel_ready("xiaohongshu"))
    reddit_ready = connected or (via_bridge and channel_ready("reddit"))
    platform_ready = {"xiaohongshu": xhs_ready, "reddit": reddit_ready}

    p0 = [
        x for x in list_sources()
        if x.get("source_type") == "agent_reach"
        and x.get("role") == "demand"
        and x.get("business_group") == "core_demand"
        and x.get("priority") == "P0"
        and x.get("platform") in platform_ready
    ]
    eligible = [x for x in p0 if platform_ready.get(str(x.get("platform")), False)]
    if not eligible:
        reason = "OpenCLI Browser Bridge 未连接，或小红书/Reddit 登录通道未就绪；P0 保持关闭。"
        return {
            "activated": 0, "eligible": 0, "ready": False, "reason": reason,
            "opencli_connected": connected, "platform_ready": platform_ready,
        }

    activated = 0
    already_enabled = 0
    now = now_iso()
    with conn() as c:
        for source in eligible:
            if source.get("enabled"):
                already_enabled += 1
                continue
            c.execute("UPDATE sources SET enabled=1,updated_at=? WHERE id=?", (now, int(source["id"])))
            activated += 1
    return {
        "activated": activated, "already_enabled": already_enabled, "eligible": len(eligible),
        "ready": True, "opencli_connected": connected, "platform_ready": platform_ready,
    }


@app.post("/api/sources/activate-core-demand")
def sources_activate_core_demand():
    result = _activate_p0_from_real_login()
    if not result.get("ready"):
        raise HTTPException(409, result.get("reason") or "P0 登录通道未就绪")
    return result


@app.post("/api/sources")
def sources_create(body: SourceCreate):
    try:
        sid = create_source(body.model_dump())
        return {"id": sid}
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(409, "source name already exists")
        raise


@app.get("/api/sources")
def sources_list():
    return list_sources()


@app.patch("/api/sources/{source_id}")
def sources_update(source_id: int, body: SourceUpdate):
    source = get_source(source_id)
    if not source:
        raise HTTPException(404, "source not found")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return {"id": source_id, "updated": False}
    fields = []
    values: list[Any] = []
    if "enabled" in updates:
        fields.append("enabled=?"); values.append(1 if updates["enabled"] else 0)
    if "query" in updates:
        fields.append("query=?"); values.append(updates["query"])
    if "url" in updates:
        fields.append("url=?"); values.append(updates["url"])
    if "config" in updates:
        fields.append("config_json=?"); values.append(json.dumps(updates["config"] or {}, ensure_ascii=False))
    fields.append("updated_at=?"); values.append(now_iso())
    values.append(source_id)
    with conn() as c:
        c.execute(f"UPDATE sources SET {','.join(fields)} WHERE id=?", tuple(values))
    return {"id": source_id, "updated": True}


@app.post("/api/sources/{source_id}/collect")
def sources_collect(source_id: int, limit: int | None = Query(default=None, ge=1, le=100), rebuild: bool = True, force: bool = False):
    try:
        result = collect_source(source_id, limit, force=force)
        if rebuild and result.get("status") in {"SUCCESS", "EMPTY"}:
            result["cluster_run"] = build_from_unlinked_evidence()
        return result
    except KeyError:
        raise HTTPException(404, "source not found")
    except Exception as exc:
        raise HTTPException(502, str(exc))


def _collect_source_batch(sources: list[dict[str, Any]], scope: str, force: bool = False) -> dict[str, Any]:
    results = []
    for source in sources:
        try:
            result = collect_source(int(source["id"]), force=force)
            result["source_name"] = source.get("name")
            result["platform"] = source.get("platform")
            result["priority"] = source.get("priority")
            results.append(result)
        except Exception as exc:
            results.append({
                "source_id": source["id"], "source_name": source.get("name"), "platform": source.get("platform"),
                "priority": source.get("priority"), "status": "FAILED", "error": str(exc),
            })
    cluster_run = None
    if any(x.get("status") in {"SUCCESS", "EMPTY"} for x in results):
        cluster_run = build_from_unlinked_evidence()
    return {
        "scope": scope,
        "processed": len(results),
        "results": results,
        "cluster_run": cluster_run,
    }


@app.post("/api/sources/collect-core-demand")
def sources_collect_core_demand(force: bool = False):
    all_sources = list_sources()
    core = [
        x for x in all_sources
        if x.get("enabled") and x.get("role") == "demand" and x.get("business_group") == "core_demand" and x.get("priority") == "P0"
    ]
    disabled_core = [
        {"id": x["id"], "name": x["name"], "platform": x["platform"], "health_status": x.get("health_status")}
        for x in all_sources
        if not x.get("enabled") and x.get("role") == "demand" and x.get("business_group") == "core_demand" and x.get("priority") == "P0"
    ]
    if not core:
        return {
            "scope": "core_demand", "processed": 0, "results": [], "cluster_run": None,
            "disabled_core_sources": disabled_core,
            "message": "没有已启用的 P0 核心需求源；先把 Agent-Reach/登录态配置好，再启用小红书或 Reddit。",
        }
    result = _collect_source_batch(core, "core_demand", force=force)
    result["disabled_core_sources"] = disabled_core
    return result


@app.post("/api/sources/collect-enabled")
def sources_collect_enabled(force: bool = False):
    sources = [x for x in list_sources() if x.get("enabled")]
    return _collect_source_batch(sources, "all_enabled", force=force)


@app.get("/api/market-intel")
def market_intel_list(
    limit: int = Query(default=100, ge=1, le=500),
    high_heat: bool = False,
    high_sales: bool = False,
    hot_and_selling: bool = False,
):
    return list_market_intel(
        limit,
        high_heat=high_heat,
        high_sales=high_sales,
        hot_and_selling=hot_and_selling,
    )


@app.get("/api/market-intel/summary")
def market_intel_summary_api():
    return market_intel_summary()


@app.post("/api/market-intel/rematch")
def market_intel_rematch():
    return {"matched_links": rematch_market_intel()}


@app.post("/api/market-intel/collect")
def market_intel_collect(force: bool = True):
    market_sources = [
        source for source in list_sources()
        if source.get("enabled") and source.get("role") == "market"
    ]
    results = []
    for source in market_sources:
        try:
            item = collect_source(int(source["id"]), force=force)
            item["source_name"] = source.get("name")
            item["platform"] = source.get("platform")
            results.append(item)
        except Exception as exc:
            results.append({
                "source_id": source.get("id"),
                "source_name": source.get("name"),
                "platform": source.get("platform"),
                "status": "FAILED",
                "error": str(exc),
            })
    return {
        "processed": len(results),
        "success_count": sum(1 for item in results if item.get("status") in {"SUCCESS", "EMPTY"}),
        "failed_count": sum(1 for item in results if item.get("status") == "FAILED"),
        "results": results,
        "summary": market_intel_summary(),
    }


@app.get("/api/agent-reach/doctor")
def agent_reach_health():
    return agent_reach_doctor()


@app.get("/api/rsshub/doctor")
def rsshub_health():
    return rsshub_doctor()


@app.get("/api/coverage")
def coverage():
    return platform_coverage()


@app.get("/api/data-quality")
def data_quality():
    return data_quality_report()


@app.get("/api/scheduler/status")
def scheduler_get_status():
    return scheduler_status()


@app.post("/api/scheduler/run-now")
def scheduler_run_now():
    return scheduler_run_cycle()


@app.get("/api/evidence")
def evidence_list(platform: str | None = None, source_id: int | None = None, intent_class: str | None = None, content_kind: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    sql = "SELECT DISTINCT e.* FROM evidence e"
    where = []
    params: list[Any] = []
    if source_id:
        sql += " JOIN evidence_sources es_filter ON es_filter.evidence_id=e.id"
        where.append("es_filter.source_id=?"); params.append(source_id)
    if platform:
        where.append("e.platform=?"); params.append(platform)
    if intent_class:
        where.append("e.intent_class=?"); params.append(intent_class)
    if content_kind:
        where.append("e.content_kind=?"); params.append(content_kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(limit)
    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
        ids = [int(r["id"]) for r in rows]
        links = []
        if ids:
            ph = ",".join("?" for _ in ids)
            links = c.execute(f"""SELECT es.evidence_id,s.id source_id,s.name source_name,s.platform source_platform,s.query,s.config_json
                                  FROM evidence_sources es JOIN sources s ON s.id=es.source_id
                                  WHERE es.evidence_id IN ({ph}) ORDER BY s.id""", tuple(ids)).fetchall()
    by_e: dict[int,list[dict[str,Any]]] = {}
    for r in links:
        d = row_to_dict(r) or {}
        cfg = loads(d.pop("config_json",None),{}) or {}
        d["role"] = cfg.get("role","demand")
        by_e.setdefault(int(d["evidence_id"]),[]).append(d)
    out = []
    for r in rows:
        d = row_to_dict(r) or {}
        d["engagement"] = loads(d.pop("engagement_json", None), {})
        d["intent_reason"] = loads(d.pop("intent_reason_json", None), [])
        d.pop("raw_json", None)
        d["source_matches"] = by_e.get(int(d["id"]),[])
        out.append(d)
    return out


@app.get("/api/evidence/{evidence_id}")
def evidence_detail(evidence_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        links = c.execute("""SELECT s.id source_id,s.name source_name,s.platform source_platform,s.query,s.config_json,es.first_seen_at,es.last_seen_at,es.observation_count
                             FROM evidence_sources es JOIN sources s ON s.id=es.source_id WHERE es.evidence_id=? ORDER BY s.id""",(evidence_id,)).fetchall()
        observations = c.execute("SELECT observed_at,engagement_json,raw_hash,source_id FROM evidence_observations WHERE evidence_id=? ORDER BY id DESC LIMIT 30",(evidence_id,)).fetchall()
    if not row:
        raise HTTPException(404, "evidence not found")
    d = row_to_dict(row) or {}
    d["engagement"] = loads(d.pop("engagement_json", None), {})
    d["raw"] = loads(d.pop("raw_json", None), {})
    d["intent_reason"] = loads(d.pop("intent_reason_json", None), [])
    d["source_matches"] = []
    for r in links:
        x = row_to_dict(r) or {}
        cfg = loads(x.pop("config_json",None),{}) or {}
        x["role"] = cfg.get("role","demand")
        d["source_matches"].append(x)
    d["observations"] = []
    for r in observations:
        x = row_to_dict(r) or {}
        x["engagement"] = loads(x.pop("engagement_json",None),{})
        d["observations"].append(x)
    return d


@app.post("/api/opportunities/build")
def opportunities_build():
    return build_from_unlinked_evidence()


@app.get("/api/opportunities")
def opportunities_list(status: str | None = None, cross_source_only: bool = False, limit: int = Query(default=100, ge=1, le=500)):
    rows = list_opportunities(limit=limit, status=status, cross_source_only=cross_source_only)
    for item in rows:
        try:
            item["gate_a"] = get_gate_a(int(item["id"]))
        except Exception as exc:
            item["gate_a"] = {"decision": "REVIEW_REQUIRED", "error": str(exc)}
    return rows


@app.get("/api/trends")
def trends_list(cross_source_only: bool = False, limit: int = Query(default=100, ge=1, le=500)):
    return trend_ranking(limit=limit, cross_source_only=cross_source_only)


@app.get("/api/trends/{opportunity_id}")
def trends_detail(opportunity_id: int):
    value = trend_detail(opportunity_id)
    if not value:
        raise HTTPException(404, "trend not calculated; rebuild opportunities first")
    return value


@app.get("/api/clusters/latest")
def cluster_latest():
    return latest_cluster_run() or {"status": "NEVER_RUN"}


@app.get("/api/opportunities/{opportunity_id}")
def opportunity_detail(opportunity_id: int):
    opp = get_opportunity(opportunity_id)
    if not opp:
        raise HTTPException(404, "opportunity not found")
    opp["analysis"] = loads(opp.get("analysis_json"), {})
    opp["trend"] = trend_detail(opportunity_id)
    opp["evidence"] = get_evidence_for_opportunity(opportunity_id, 100)
    opp["validation"] = get_validation(opportunity_id)
    opp["market_intel"] = market_intel_for_opportunity(opportunity_id, 20)
    return opp


@app.get("/api/opportunities/{opportunity_id}/validation")
def opportunity_validation_get(opportunity_id: int):
    try:
        return get_validation(opportunity_id)
    except KeyError:
        raise HTTPException(404, "opportunity not found")


@app.post("/api/opportunities/{opportunity_id}/validation")
def opportunity_validation_run(opportunity_id: int):
    try:
        return validate_opportunity(opportunity_id)
    except KeyError:
        raise HTTPException(404, "opportunity not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/opportunities/batch/validate")
def opportunities_validate_batch(limit: int = Query(default=100, ge=1, le=500)):
    return validate_active_opportunities(limit)


@app.post("/api/opportunities/{opportunity_id}/analyze")
def opportunity_analyze(opportunity_id: int):
    try:
        return analyze_one(opportunity_id)
    except KeyError:
        raise HTTPException(404, "opportunity not found")
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/opportunities/batch/analyze-new")
def opportunities_analyze_new():
    with conn() as c:
        rows = c.execute("SELECT id FROM opportunities WHERE cluster_active=1 AND (status='NEW' OR analysis_stale=1) AND evidence_count>=? ORDER BY coalesce(trusted_trend_score,0) DESC,coalesce(trend_score,0) DESC,evidence_count DESC,id LIMIT ?", (settings.min_evidence_for_analysis, settings.max_ai_items_per_batch)).fetchall()
    results = []
    for r in rows:
        oid = int(r["id"])
        try:
            results.append(analyze_one(oid))
        except Exception as exc:
            results.append({"id": oid, "error": str(exc)})
    return {"processed": len(results), "results": results}


@app.post("/api/products/from-opportunity/{opportunity_id}")
def products_generate(opportunity_id: int):
    try:
        return product_from_opportunity(opportunity_id)
    except KeyError:
        raise HTTPException(404, "opportunity not found")
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/source-assets")
def source_assets_list(limit: int = Query(default=100, ge=1, le=500)):
    return list_source_assets(limit)


@app.post("/api/source-assets")
def source_asset_create(body: SourceAssetCreate):
    try:
        return create_source_asset(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/source-assets/{asset_id}")
def source_asset_detail(asset_id: int):
    asset = get_source_asset(asset_id)
    if not asset:
        raise HTTPException(404, "source asset not found")
    return asset


@app.patch("/api/source-assets/{asset_id}")
def source_asset_update(asset_id: int, body: SourceAssetUpdate):
    try:
        return update_source_asset(asset_id, body.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(404, "source asset not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/products/{product_id}/assets")
def product_assets_list(product_id: int):
    try:
        return {"assets": list_product_assets(product_id), "license_readiness": product_asset_readiness(product_id)}
    except KeyError:
        raise HTTPException(404, "product not found")


@app.post("/api/products/{product_id}/assets/{asset_id}")
def product_asset_link(product_id: int, asset_id: int, role: str = Query(default="deliverable")):
    try:
        return link_product_asset(product_id, asset_id, role)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/products/{product_id}/assets/{asset_id}")
def product_asset_unlink(product_id: int, asset_id: int, role: str | None = None):
    try:
        return unlink_product_asset(product_id, asset_id, role)
    except KeyError:
        raise HTTPException(404, "product not found")


@app.get("/api/products/{product_id}/license-readiness")
def product_license_readiness(product_id: int):
    try:
        return product_asset_readiness(product_id)
    except KeyError:
        raise HTTPException(404, "product not found")


@app.get("/api/products")
def products_list(limit: int = Query(default=100, ge=1, le=500)):
    with conn() as c:
        rows = c.execute("SELECT id FROM products ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        product = get_product(int(row["id"]))
        if not product:
            continue
        try:
            product["gate_b"] = get_gate_b(int(row["id"]))
        except Exception as exc:
            product["gate_b"] = {"decision": "REVIEW_REQUIRED", "is_current": False, "error": str(exc)}
        try:
            product["license_readiness"] = product_asset_readiness(int(row["id"]))
        except Exception as exc:
            product["license_readiness"] = {"decision": "REVIEW_REQUIRED", "error": str(exc)}
        product["builds"] = list_product_builds(int(row["id"]), 1)
        product["publish_runs"] = list_publish_runs(int(row["id"]), 1)
        out.append(product)
    return out


@app.get("/api/products/{product_id}")
def product_detail(product_id: int):
    product = get_product(product_id)
    if not product:
        raise HTTPException(404, "product not found")
    try:
        product["gate_b"] = get_gate_b(product_id)
    except Exception:
        product["gate_b"] = {"product_id": product_id, "decision": "NOT_RUN", "is_current": False}
    try:
        product["assets"] = list_product_assets(product_id)
        product["license_readiness"] = product_asset_readiness(product_id)
    except Exception as exc:
        product["assets"] = []
        product["license_readiness"] = {"decision": "REVIEW_REQUIRED", "error": str(exc)}
    product["builds"] = list_product_builds(product_id, 20)
    product["publish_runs"] = list_publish_runs(product_id, 20)
    return product


@app.post("/api/products/{product_id}/build-artifact")
def product_build_artifact(product_id: int):
    try:
        return build_product_artifact(product_id)
    except KeyError:
        raise HTTPException(404, "product not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/products/{product_id}/builds")
def product_builds(product_id: int, limit: int = Query(default=20, ge=1, le=100)):
    if not get_product(product_id):
        raise HTTPException(404, "product not found")
    return list_product_builds(product_id, limit)


@app.get("/api/products/{product_id}/artifact/download")
def product_artifact_download(product_id: int):
    try:
        build = latest_successful_bundle(product_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    path = Path(str(build["bundle_path"]))
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/api/publish/etsy/status")
def publish_etsy_status():
    return etsy_status()


@app.get("/api/products/{product_id}/publish-runs")
def product_publish_runs(product_id: int, limit: int = Query(default=20, ge=1, le=100)):
    if not get_product(product_id):
        raise HTTPException(404, "product not found")
    return list_publish_runs(product_id, limit)


@app.post("/api/products/{product_id}/publish/etsy")
def product_publish_etsy(
    product_id: int,
    price: float | None = Query(default=None, gt=0),
    currency: str | None = Query(default=None, min_length=3, max_length=3),
    taxonomy_id: int | None = Query(default=None, gt=0),
    quantity: int | None = Query(default=None, gt=0),
    activate: bool = True,
):
    try:
        return publish_to_etsy(
            product_id,
            price=price,
            currency=currency,
            taxonomy_id=taxonomy_id,
            quantity=quantity,
            activate=activate,
        )
    except KeyError:
        raise HTTPException(404, "product not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:2000] if exc.response is not None else str(exc)
        raise HTTPException(502, f"Etsy API error: {detail}")
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/products/{product_id}/gate-b")
def product_gate_b_get(product_id: int):
    try:
        return get_gate_b(product_id)
    except KeyError:
        raise HTTPException(404, "product not found")


@app.post("/api/products/{product_id}/gate-b")
def product_gate_b_run(product_id: int):
    try:
        return evaluate_gate_b(product_id)
    except KeyError:
        raise HTTPException(404, "product not found")


@app.post("/api/products/{product_id}/approve")
def product_approve(product_id: int):
    try:
        return approve_product(product_id)
    except KeyError:
        raise HTTPException(404, "product not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/ai/cost")
def ai_cost(limit: int = Query(default=100, ge=1, le=500)):
    with conn() as c:
        rows = c.execute("SELECT * FROM ai_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        total = c.execute("SELECT coalesce(sum(estimated_cost_cny),0) s FROM ai_runs WHERE status='SUCCESS'").fetchone()["s"]
    return {"total_cost_cny": round(float(total or 0), 6), "daily_budget_cny": settings.daily_ai_budget_cny, "runs": [row_to_dict(r) for r in rows]}


@app.get("/api/deliveries")
def deliveries_list(limit: int = Query(default=100, ge=1, le=500)):
    return list_delivery_records(limit)


@app.post("/api/deliveries")
def delivery_create(body: DeliveryIn):
    try:
        require_approved_current_product(body.product_id)
    except KeyError:
        raise HTTPException(404, "product not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    now = now_iso()
    resource = dict(body.resource or {})
    if body.delivery_uri and "uri" not in resource:
        resource["uri"] = body.delivery_uri
    delivered = bool(body.delivery_uri or resource) and body.delivery_type != "manual_review"
    status = "DELIVERED" if delivered else "PENDING"
    delivered_at = now if delivered else None
    with conn() as c:
        try:
            cur = c.execute(
                """INSERT INTO deliveries(
                       product_id,platform,order_ref,buyer_ref,delivery_uri,delivery_type,resource_json,retry_count,error_message,delivered_at,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    body.product_id, body.platform, body.order_ref, body.buyer_ref, body.delivery_uri,
                    body.delivery_type, dumps(resource) if resource else None, 0, None, delivered_at, status, now, now,
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise HTTPException(409, "order already exists")
            raise
    return {
        "id": int(cur.lastrowid),
        "status": status,
        "delivery_type": body.delivery_type,
        "delivered_at": delivered_at,
    }
