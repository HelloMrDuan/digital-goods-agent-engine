from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..collectors import (
    collect_agent_reach,
    collect_bilibili,
    collect_github,
    collect_google_news,
    collect_rss,
    collect_rsshub,
    collect_v2ex,
    collect_youtube,
    collect_creative_market,
    collect_etsy_market,
    collect_xianyu_market,
)
from ..collectors.base import CollectorError
from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict
from .ingest import ingest_items
from .asset_registry import sync_github_source_asset_candidates
from .market_intel import sync_market_items

_source_locks: dict[int, threading.Lock] = {}
_source_locks_guard = threading.Lock()


def _source_lock(source_id: int) -> threading.Lock:
    with _source_locks_guard:
        return _source_locks.setdefault(source_id, threading.Lock())


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_source(source_id: int) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return row_to_dict(row)


def source_role(source: dict[str, Any]) -> str:
    cfg = loads(source.get("config_json"), {}) if "config_json" in source else source.get("config", {})
    cfg = cfg or {}
    role = str(cfg.get("role") or "demand").lower()
    return role if role in {"demand", "trend", "supply", "market"} else "demand"


def source_priority(source: dict[str, Any]) -> str:
    cfg = loads(source.get("config_json"), {}) if "config_json" in source else source.get("config", {})
    cfg = cfg or {}
    explicit = cfg.get("priority")
    if explicit:
        priority = str(explicit).upper()
        return priority if priority in {"P0", "P1", "P2", "P3"} else "P3"
    role = str(cfg.get("role") or "demand").lower()
    platform = str(source.get("platform") or "").lower()
    if platform == "v2ex":
        return "P3"
    return {"demand": "P0", "trend": "P1", "market": "P1", "supply": "P2"}.get(role, "P3")


def source_business_group(source: dict[str, Any]) -> str:
    cfg = loads(source.get("config_json"), {}) if "config_json" in source else source.get("config", {})
    cfg = cfg or {}
    explicit = cfg.get("business_group")
    if explicit:
        group = str(explicit)
        return group if group in {"core_demand", "trend_support", "market_intel", "supply_intel", "auxiliary"} else "auxiliary"
    role = str(cfg.get("role") or "demand").lower()
    platform = str(source.get("platform") or "").lower()
    if platform == "v2ex":
        return "auxiliary"
    return {"demand": "core_demand", "trend": "trend_support", "market": "market_intel", "supply": "supply_intel"}.get(role, "auxiliary")


def create_source(data: dict[str, Any]) -> int:
    now = now_iso()
    config = data.get("config") or {}
    role = str(config.get("role") or "demand").lower()
    if role not in {"demand", "trend", "supply", "market"}:
        raise ValueError("source config.role must be demand, trend, market or supply")
    with conn() as c:
        cur = c.execute(
            """INSERT INTO sources(name,source_type,platform,query,url,config_json,enabled,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"], data["source_type"], data["platform"], data.get("query"), data.get("url"),
                dumps(config), 1 if data.get("enabled", True) else 0, "NOT_CHECKED", now, now,
            ),
        )
        return int(cur.lastrowid)


def _freshness_status(source: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not source.get("enabled"):
        return "DISABLED"
    retry = _parse_dt(source.get("next_retry_at"))
    if retry and retry > now:
        return "BACKOFF"
    status = str(source.get("status") or "NOT_CHECKED")
    if status in {"ERROR", "EMPTY", "NOT_CHECKED"}:
        return status
    last = _parse_dt(source.get("last_success_at"))
    if not last:
        return "NOT_CHECKED"
    if now - last > timedelta(hours=max(1, settings.source_stale_after_hours)):
        return "STALE"
    return "READY"


def list_sources() -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            """
            SELECT s.*,
                   COUNT(DISTINCT es.evidence_id) AS evidence_count,
                   MAX(es.last_seen_at) AS evidence_last_seen_at,
                   AVG(CASE WHEN e.evidence_quality_score>0 THEN e.evidence_quality_score END) AS avg_evidence_quality
            FROM sources s
            LEFT JOIN evidence_sources es ON es.source_id=s.id
            LEFT JOIN evidence e ON e.id=es.evidence_id
            GROUP BY s.id
            ORDER BY s.id
            """
        ).fetchall()
    result = []
    for row in rows:
        d = row_to_dict(row) or {}
        d["config"] = loads(d.pop("config_json", None), {})
        d["role"] = str(d["config"].get("role") or "demand")
        d["priority"] = source_priority(d)
        d["business_group"] = source_business_group(d)
        d["recommended"] = bool(d["config"].get("recommended", False))
        d["health_status"] = _freshness_status(d)
        result.append(d)
    return result


def _dispatch(source: dict[str, Any], config: dict[str, Any], limit: int):
    t = source["source_type"]
    if t == "rss":
        if not source.get("url"):
            raise CollectorError("RSS source missing url")
        return collect_rss(source["url"], platform=source["platform"], limit=limit)
    if t == "rsshub":
        route = source.get("url") or config.get("route")
        if not route:
            raise CollectorError("RSSHub source missing url/config.route")
        return collect_rsshub(str(route), platform=source["platform"], limit=limit)
    if t == "google_news":
        if not source.get("query"):
            raise CollectorError("Google News source missing query")
        return collect_google_news(source["query"], limit=limit, **{k: config[k] for k in ("hl", "gl", "ceid") if k in config})
    if t == "github":
        if not source.get("query"):
            raise CollectorError("GitHub source missing query")
        return collect_github(source["query"], limit=limit, mode=config.get("mode", "repositories"))
    if t == "v2ex":
        return collect_v2ex(mode=config.get("mode", "hot"), limit=limit, node_name=config.get("node_name"))
    if t == "agent_reach":
        if not source.get("query"):
            raise CollectorError("Agent-Reach source missing query")
        return collect_agent_reach(source["platform"], source["query"], limit=limit, enrich=bool(config.get("enrich", False)), detail_limit=int(config.get("detail_limit", 8)), comments_limit=int(config.get("comments_limit", 15)))
    if t == "bilibili":
        if not source.get("query"):
            raise CollectorError("Bilibili source missing query")
        return collect_bilibili(source["query"], limit=limit)
    if t == "youtube":
        if not source.get("query"):
            raise CollectorError("YouTube source missing query")
        return collect_youtube(source["query"], limit=limit)
    if t == "creative_market":
        return collect_creative_market(
            str(source.get("url") or ""),
            limit=limit,
            ranking=str(config.get("ranking") or "bestseller"),
        )
    if t == "etsy_market":
        if not source.get("query"):
            raise CollectorError("Etsy market source missing query")
        return collect_etsy_market(str(source["query"]), limit=limit)
    if t == "xianyu_market":
        if not source.get("query"):
            raise CollectorError("Xianyu market source missing query")
        return collect_xianyu_market(str(source["query"]), limit=limit)
    raise CollectorError(f"unsupported source_type: {t}")


def _non_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(x in text for x in ("missing query", "missing url", "unsupported source_type", "is disabled", "not configured", "not installed"))


def _due(source: dict[str, Any], config: dict[str, Any], force: bool) -> tuple[bool, str | None]:
    if force:
        return True, None
    now = datetime.now(timezone.utc)
    retry = _parse_dt(source.get("next_retry_at"))
    if retry and retry > now:
        return False, f"backoff until {retry.isoformat(timespec='seconds')}"
    min_interval = max(0, int(config.get("min_interval_minutes") or 0))
    last = _parse_dt(source.get("last_checked_at"))
    if min_interval and last and now - last < timedelta(minutes=min_interval):
        due_at = last + timedelta(minutes=min_interval)
        return False, f"minimum interval; next due {due_at.isoformat(timespec='seconds')}"
    return True, None


def collect_source(source_id: int, limit: int | None = None, force: bool = False) -> dict[str, Any]:
    lock = _source_lock(source_id)
    if not lock.acquire(blocking=False):
        return {"source_id": source_id, "status": "SKIPPED", "reason": "source collection already running"}
    try:
        source = get_source(source_id)
        if not source:
            raise KeyError("source not found")
        if not source.get("enabled"):
            raise CollectorError("source is disabled")
        config = loads(source.get("config_json"), {}) or {}
        due, reason = _due(source, config, force)
        if not due:
            return {"source_id": source_id, "status": "SKIPPED", "reason": reason, "role": source_role(source)}

        limit = int(limit or config.get("limit") or settings.collector_limit_default)
        limit = max(1, min(limit, 100))
        run_started = now_iso()
        t0 = time.monotonic()
        with conn() as c:
            cur = c.execute("INSERT INTO collection_runs(source_id,status,started_at) VALUES(?,?,?)", (source_id, "RUNNING", run_started))
            run_id = int(cur.lastrowid)

        retries = max(0, int(config.get("retries", settings.collector_retries)))
        retry_count = 0
        last_exc: Exception | None = None
        items = None
        for attempt in range(retries + 1):
            try:
                items = _dispatch(source, config, limit)
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= retries or _non_retryable(exc):
                    break
                retry_count += 1
                time.sleep(max(0.0, settings.collector_backoff_base_seconds) * (2 ** attempt))
        if items is None:
            assert last_exc is not None
            raise last_exc

        source_asset_sync = None
        if (
            source.get("source_type") == "github"
            and source_role(source) == "supply"
            and source_priority(source) == "P2"
            and str(config.get("mode") or "repositories") == "repositories"
        ):
            source_asset_sync = sync_github_source_asset_candidates(source, items)

        market_intel_sync = None
        if source_role(source) == "market":
            market_intel_sync = sync_market_items(source, items)
            stats = market_intel_sync
        else:
            stats = ingest_items(source, items)
        finished = now_iso()
        duration_ms = max(0, int((time.monotonic() - t0) * 1000))
        empty = stats["fetched"] == 0
        status = "EMPTY" if empty else "SUCCESS"
        source_status = "EMPTY" if empty else "READY"
        old_avg = float(source.get("avg_duration_ms") or 0)
        avg_ms = duration_ms if old_avg <= 0 else round(old_avg * 0.8 + duration_ms * 0.2, 2)
        with conn() as c:
            c.execute(
                """UPDATE collection_runs SET status=?, finished_at=?, fetched_count=?, inserted_count=?, duplicate_count=?,
                   rejected_count=?,retry_count=?,duration_ms=? WHERE id=?""",
                (status, finished, stats["fetched"], stats["inserted"], stats["duplicates"], stats["rejected_invalid"], retry_count, duration_ms, run_id),
            )
            c.execute(
                """UPDATE sources SET status=?, last_error=NULL, last_checked_at=?, last_success_at=?, last_item_count=?,
                   failure_streak=0,empty_streak=?,next_retry_at=NULL,avg_duration_ms=?,updated_at=? WHERE id=?""",
                (source_status, finished, finished, stats["fetched"], int(source.get("empty_streak") or 0) + 1 if empty else 0, avg_ms, finished, source_id),
            )
        return {
            "run_id": run_id,
            "source_id": source_id,
            "status": status,
            "role": source_role(source),
            "duration_ms": duration_ms,
            "retry_count": retry_count,
            "source_asset_sync": source_asset_sync,
            "market_intel_sync": market_intel_sync,
            **stats,
        }
    except Exception as exc:
        finished = now_iso()
        duration_ms = max(0, int((time.monotonic() - t0) * 1000)) if 't0' in locals() else 0
        error = str(exc)
        source = get_source(source_id) or {}
        streak = int(source.get("failure_streak") or 0) + 1
        base = max(1, int(settings.collector_failure_backoff_minutes))
        backoff_m = min(max(base, base * (2 ** max(0, streak - 1))), max(base, int(settings.collector_failure_backoff_max_minutes)))
        next_retry = (datetime.now(timezone.utc) + timedelta(minutes=backoff_m)).isoformat(timespec="seconds")
        with conn() as c:
            if 'run_id' in locals():
                c.execute("UPDATE collection_runs SET status='FAILED', finished_at=?, retry_count=?,duration_ms=?, error=? WHERE id=?", (finished, retry_count if 'retry_count' in locals() else 0, duration_ms, error, run_id))
            c.execute(
                """UPDATE sources SET status='ERROR',last_error=?,last_checked_at=?,failure_streak=?,next_retry_at=?,updated_at=? WHERE id=?""",
                (error, finished, streak, next_retry, finished, source_id),
            )
        raise
    finally:
        lock.release()


def platform_coverage() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cut24 = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    cut7 = (now - timedelta(days=7)).isoformat(timespec="seconds")
    with conn() as c:
        rows = c.execute(
            """
            SELECT s.platform,
                   COUNT(DISTINCT s.id) AS source_count,
                   SUM(CASE WHEN s.enabled=1 THEN 1 ELSE 0 END) AS enabled_source_count,
                   SUM(CASE WHEN s.status='READY' THEN 1 ELSE 0 END) AS ready_source_count,
                   SUM(CASE WHEN s.status='ERROR' THEN 1 ELSE 0 END) AS error_source_count,
                   MAX(s.last_success_at) AS last_success_at,
                   MAX(s.failure_streak) AS max_failure_streak,
                   COUNT(DISTINCT es.evidence_id) AS evidence_count,
                   COUNT(DISTINCT CASE WHEN e.intent_class IN ('explicit_demand','probable_demand') THEN es.evidence_id END) AS demand_eligible_count,
                   COUNT(DISTINCT CASE WHEN e.intent_class='explicit_demand' THEN es.evidence_id END) AS explicit_demand_count,
                   COUNT(DISTINCT CASE WHEN e.intent_class='probable_demand' THEN es.evidence_id END) AS probable_demand_count,
                   COUNT(DISTINCT CASE WHEN e.content_kind='comment' AND e.intent_class IN ('explicit_demand','probable_demand') THEN es.evidence_id END) AS comment_demand_count,
                   COUNT(DISTINCT CASE WHEN e.collected_at>=? THEN es.evidence_id END) AS evidence_24h,
                   COUNT(DISTINCT CASE WHEN e.collected_at>=? THEN es.evidence_id END) AS evidence_7d,
                   AVG(CASE WHEN e.evidence_quality_score>0 THEN e.evidence_quality_score END) AS avg_evidence_quality
            FROM sources s
            LEFT JOIN evidence_sources es ON es.source_id=s.id
            LEFT JOIN evidence e ON e.id=es.evidence_id
            GROUP BY s.platform
            ORDER BY evidence_7d DESC, s.platform
            """,
            (cut24, cut7),
        ).fetchall()
        source_rows = c.execute("SELECT * FROM sources ORDER BY id").fetchall()
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for r in source_rows:
        d = row_to_dict(r) or {}
        cfg = loads(d.get("config_json"), {}) or {}
        by_platform.setdefault(str(d["platform"]), []).append({
            "id": d["id"], "name": d["name"], "role": cfg.get("role", "demand"), "enabled": bool(d["enabled"]),
            "priority": str(cfg.get("priority") or "P3").upper(),
            "business_group": str(cfg.get("business_group") or "auxiliary"),
            "recommended": bool(cfg.get("recommended", False)),
            "health_status": _freshness_status(d, now), "last_error": d.get("last_error"), "last_success_at": d.get("last_success_at"),
            "failure_streak": d.get("failure_streak", 0), "empty_streak": d.get("empty_streak", 0), "next_retry_at": d.get("next_retry_at"),
        })
    platforms = []
    for r in rows:
        d = row_to_dict(r) or {}
        details = by_platform.get(str(d["platform"]), [])
        d["sources"] = details
        states = {x["health_status"] for x in details if x["enabled"]}
        if "READY" in states:
            d["status"] = "READY"
        elif "BACKOFF" in states:
            d["status"] = "BACKOFF"
        elif "ERROR" in states:
            d["status"] = "ERROR"
        elif "EMPTY" in states:
            d["status"] = "EMPTY"
        elif states:
            d["status"] = sorted(states)[0]
        else:
            d["status"] = "DISABLED"
        d["roles"] = sorted({x["role"] for x in details})
        priorities = sorted({x.get("priority", "P3") for x in details}, key=lambda x: int(x[1:]) if len(x)>1 and x[1:].isdigit() else 9)
        groups = sorted({x.get("business_group", "auxiliary") for x in details})
        d["priority"] = priorities[0] if priorities else "P3"
        d["business_groups"] = groups
        platforms.append(d)
    return {"generated_at": now_iso(), "stale_after_hours": settings.source_stale_after_hours, "platforms": platforms}


def bootstrap_sources() -> dict[str, int]:
    """Create/update recommended real source configurations; never inserts evidence/business rows.

    Business priority is authoritative for recommendations:
    P0 = direct demand, P1 = trend support, P2 = supply intelligence, P3 = auxiliary observation.
    Existing user enablement is preserved for recommended sources, except legacy V2EX defaults are
    deliberately disabled because they are low-value for digital-product demand discovery.
    """
    def cfg(role: str, priority: str, group: str, min_interval: int, **extra: Any) -> dict[str, Any]:
        return {
            "role": role,
            "priority": priority,
            "business_group": group,
            "recommended": True,
            "strategy_version": "business-priority-v2-intent-gate",
            "min_interval_minutes": min_interval,
            **extra,
        }

    defaults = [
        # P0: direct demand. Kept disabled until Agent-Reach / real login-state backends are ready.
        {"name":"小红书 - 求Excel模板","source_type":"agent_reach","platform":"xiaohongshu","query":"求 Excel 模板","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},
        {"name":"小红书 - 求效率工具","source_type":"agent_reach","platform":"xiaohongshu","query":"有没有 效率 工具 推荐","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},
        {"name":"小红书 - 求职简历痛点","source_type":"agent_reach","platform":"xiaohongshu","query":"简历 求职 求模板","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},
        {"name":"小红书 - 店铺经营表格","source_type":"agent_reach","platform":"xiaohongshu","query":"求 店铺 经营 表格 模板","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},
        {"name":"小红书 - 短视频运营痛点","source_type":"agent_reach","platform":"xiaohongshu","query":"短视频 运营 有没有 工具","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},
        {"name":"Reddit - productivity template demand","source_type":"agent_reach","platform":"reddit","query":"looking for productivity template tool","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},
        {"name":"Reddit - small business spreadsheet demand","source_type":"agent_reach","platform":"reddit","query":"small business spreadsheet template looking for","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},
        {"name":"Reddit - creator workflow demand","source_type":"agent_reach","platform":"reddit","query":"looking for content creator workflow template","config":cfg("demand","P0","core_demand",180,limit=30,enrich=True,detail_limit=8,comments_limit=15),"enabled":False},

        # P1: real trend/support sources. Search/video/news results are NOT counted as direct demand.
        {"name":"Google News - Excel模板与自动化","source_type":"google_news","platform":"google_news","query":"Excel 模板 自动化 工具","config":cfg("trend","P1","trend_support",180,limit=50),"enabled":True},
        {"name":"Google News - 简历求职工具","source_type":"google_news","platform":"google_news","query":"简历 模板 求职 工具","config":cfg("trend","P1","trend_support",180,limit=50),"enabled":True},
        {"name":"Google News - 短视频运营工具","source_type":"google_news","platform":"google_news","query":"短视频 运营 工具 模板","config":cfg("trend","P1","trend_support",180,limit=50),"enabled":True},
        {"name":"B站 - Excel自动化","source_type":"bilibili","platform":"bilibili","query":"Excel 自动化 模板","config":cfg("trend","P1","trend_support",120,limit=30),"enabled":False},
        {"name":"B站 - AI副业工具","source_type":"bilibili","platform":"bilibili","query":"AI 副业 工具","config":cfg("trend","P1","trend_support",120,limit=30),"enabled":False},
        {"name":"YouTube - digital product workflow","source_type":"youtube","platform":"youtube","query":"digital product workflow automation","config":cfg("trend","P1","trend_support",360,limit=25),"enabled":False},
        {"name":"RSSHub - B站综合热门","source_type":"rsshub","platform":"bilibili_hot","url":"/bilibili/popular/all","config":cfg("trend","P1","trend_support",120,limit=50),"enabled":False},
        {"name":"RSSHub - B站热搜","source_type":"rsshub","platform":"bilibili_hot_search","url":"/bilibili/hot-search","config":cfg("trend","P1","trend_support",60,limit=50),"enabled":False},

        # P1 Market Intelligence: real marketplace ranking/sales observations.
        # These do not count as direct user demand and never bypass Gate A.
        {"name":"Creative Market - Bestselling Templates","source_type":"creative_market","platform":"creative_market","url":"https://creativemarket.com/templates-themes/popular","config":cfg("market","P1","market_intel",180,ranking="bestseller",limit=40),"enabled":True},
        {"name":"Creative Market - Trending Templates","source_type":"creative_market","platform":"creative_market","url":"https://creativemarket.com/templates-themes/trending","config":cfg("market","P1","market_intel",180,ranking="trending",limit=40),"enabled":True},
        {"name":"Etsy - Digital Template Market","source_type":"etsy_market","platform":"etsy","query":"digital download template","config":cfg("market","P1","market_intel",240,limit=30,digital_only=True),"enabled":True},
        {"name":"闲鱼 - Excel数字模板","source_type":"xianyu_market","platform":"xianyu","query":"Excel模板","config":cfg("market","P1","market_intel",180,limit=30,digital_only=True,requires_login=True),"enabled":True},
        {"name":"闲鱼 - 简历数字模板","source_type":"xianyu_market","platform":"xianyu","query":"简历模板","config":cfg("market","P1","market_intel",180,limit=30,digital_only=True,requires_login=True),"enabled":True},
        {"name":"闲鱼 - 自动化数字工具","source_type":"xianyu_market","platform":"xianyu","query":"自动化脚本","config":cfg("market","P1","market_intel",180,limit=30,digital_only=True,requires_login=True),"enabled":True},

        # P2: supply intelligence; useful for finding reusable/open-source building blocks, never demand.
        {"name":"GitHub - automation templates","source_type":"github","platform":"github","query":"automation template in:name,description","config":cfg("supply","P2","supply_intel",360,mode="repositories",limit=50),"enabled":True},
        {"name":"GitHub - digital product tools","source_type":"github","platform":"github","query":"digital product automation in:name,description","config":cfg("supply","P2","supply_intel",360,mode="repositories",limit=50),"enabled":True},
        {"name":"RSSHub - GitHub Trending Daily","source_type":"rsshub","platform":"github_trending","url":"/github/trending/daily","config":cfg("supply","P2","supply_intel",180,limit=50),"enabled":False},
    ]

    legacy_low_value = {
        "V2EX 问与答": {**cfg("demand", "P3", "auxiliary", 180, mode="node", node_name="qna", limit=60), "recommended": False},
        "V2EX 程序员": {**cfg("demand", "P3", "auxiliary", 180, mode="node", node_name="programmers", limit=60), "recommended": False},
        "V2EX 最新讨论": {**cfg("demand", "P3", "auxiliary", 180, mode="latest", limit=60), "recommended": False},
    }

    created = updated = preserved = disabled_legacy = 0
    now = now_iso()
    with conn() as c:
        for d in defaults:
            row = c.execute("SELECT * FROM sources WHERE name=?", (d["name"],)).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO sources(name,source_type,platform,query,url,config_json,enabled,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (d["name"], d["source_type"], d["platform"], d.get("query"), d.get("url"), dumps(d["config"]), 1 if d.get("enabled") else 0, "NOT_CHECKED", now, now),
                )
                created += 1
                continue
            existing = dict(row)
            # Preserve manual enabled/disabled choice on already-present recommended sources.
            c.execute(
                """UPDATE sources SET source_type=?,platform=?,query=?,url=?,config_json=?,updated_at=? WHERE id=?""",
                (d["source_type"], d["platform"], d.get("query"), d.get("url"), dumps(d["config"]), now, existing["id"]),
            )
            updated += 1
            preserved += 1

        # Explicit migration: old V2EX defaults are low-value and are no longer recommended/enabled.
        for name, config in legacy_low_value.items():
            row = c.execute("SELECT id,enabled FROM sources WHERE name=?", (name,)).fetchone()
            if row is None:
                continue
            if int(row["enabled"] or 0) != 0:
                disabled_legacy += 1
            c.execute(
                """UPDATE sources SET enabled=0,config_json=?,updated_at=? WHERE id=?""",
                (dumps(config), now, int(row["id"])),
            )

    return {
        "created": created,
        "updated": updated,
        "preserved_enablement": preserved,
        "disabled_legacy_v2ex": disabled_legacy,
        "strategy_version": "business-priority-v2-intent-gate",
    }
