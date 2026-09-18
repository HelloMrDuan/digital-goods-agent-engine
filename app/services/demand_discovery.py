from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..collectors import agent_reach_doctor, collect_agent_reach
from ..collectors.base import CollectedItem
from ..db import conn, dumps, loads, now_iso, row_to_dict
from .ingest import ingest_items

STRATEGY_VERSION = "demand-discovery-v3-intent-fanout"
MAX_AGE_DAYS = 30
MAX_WORKERS = 2

# Do not search product categories first. Search the language people use when they have
# an unmet need or payment intent, then let clustering discover the product category.
_PROBES: tuple[dict[str, Any], ...] = (
    {
        "name": "小红书 - 直接需求扫描 v3",
        "platform": "xiaohongshu",
        "limit": 60,
        "per_query_limit": 12,
        "queries": (
            "求 模板", "有没有 好用 工具", "求 表格 模板", "想找 模板",
            "有没有办法 自动", "不会做 求助", "求 运营 模板", "求 效率 工具",
        ),
    },
    {
        "name": "小红书 - 付费意图扫描 v3",
        "platform": "xiaohongshu",
        "limit": 50,
        "per_query_limit": 10,
        "queries": (
            "付费 求 模板", "愿意付费 求助", "预算 求 工具", "哪里买 模板",
            "付费 求 表格", "求靠谱 模板", "求定制 模板",
        ),
    },
    {
        "name": "Reddit - direct demand scan v3",
        "platform": "reddit",
        "limit": 60,
        "per_query_limit": 12,
        "queries": (
            "looking for a template", "need a spreadsheet template", "looking for a tool to automate",
            "any recommendations for a template", "how do you automate this workflow", "need a checklist",
            "need a notion template", "trying to find a tool",
        ),
    },
    {
        "name": "Reddit - purchase intent scan v3",
        "platform": "reddit",
        "limit": 50,
        "per_query_limit": 10,
        "queries": (
            "willing to pay for template", "where can I buy a template", "best paid template",
            "paid tool recommendation", "budget for a tool", "would pay for automation",
            "paid spreadsheet template",
        ),
    },
)

_LEGACY_NARROW_P0 = {
    "小红书 - 求Excel模板", "小红书 - 求效率工具", "小红书 - 求职简历痛点",
    "小红书 - 店铺经营表格", "小红书 - 短视频运营痛点",
    "Reddit - productivity template demand", "Reddit - small business spreadsheet demand",
    "Reddit - creator workflow demand",
}


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit():
            raw = int(text)
            if raw > 10_000_000_000:
                raw = raw / 1000
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _item_key(item: CollectedItem) -> tuple[str, str]:
    if item.source_item_id not in (None, ""):
        return item.platform, f"id:{item.source_item_id}"
    if item.source_url:
        return item.platform, f"url:{item.source_url}"
    return item.platform, f"fallback:{item.author or ''}:{item.title}"


def _prepare_items(items: list[CollectedItem], limit: int) -> list[CollectedItem]:
    """Reject stale/undated hits and prefer independent real authors.

    Search backends are relevance-ranked, so without this step an old highly-ranked post
    can dominate a candidate even when there is no current market activity.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    unique: dict[tuple[str, str], CollectedItem] = {}
    for item in items:
        published = _parse_dt(item.published_at)
        if published is None or published < cutoff or published > now + timedelta(days=1):
            continue
        unique.setdefault(_item_key(item), item)

    ranked = list(unique.values())
    ranked.sort(key=lambda item: _parse_dt(item.published_at) or cutoff, reverse=True)

    chosen: list[CollectedItem] = []
    deferred: list[CollectedItem] = []
    authors: set[str] = set()
    for item in ranked:
        author = str(item.author or "").strip().lower()
        if author and author not in authors:
            authors.add(author)
            chosen.append(item)
        else:
            deferred.append(item)
        if len(chosen) >= limit:
            return chosen[:limit]
    for item in deferred:
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


def _probe_config(probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "demand",
        "priority": "P0",
        "business_group": "core_demand",
        "recommended": True,
        "managed_refresh": True,
        "strategy_version": STRATEGY_VERSION,
        "max_age_days": MAX_AGE_DAYS,
        "query_variants": list(probe["queries"]),
    }


def install_sources() -> dict[str, int]:
    """Install v3 broad demand probes and retire the narrow v2 probes.

    Historical evidence is preserved. Only future collection strategy changes.
    """
    now = now_iso()
    created = updated = retired = 0
    with conn() as c:
        for name in _LEGACY_NARROW_P0:
            row = c.execute("SELECT id,config_json FROM sources WHERE name=?", (name,)).fetchone()
            if not row:
                continue
            cfg = loads(row["config_json"], {}) or {}
            cfg.update({
                "role": "demand",
                "priority": "P3",
                "business_group": "auxiliary",
                "recommended": False,
                "strategy_version": "retired-narrow-demand-v2",
            })
            c.execute(
                "UPDATE sources SET enabled=0,config_json=?,updated_at=? WHERE id=?",
                (dumps(cfg), now, int(row["id"])),
            )
            retired += 1

        for probe in _PROBES:
            cfg = _probe_config(probe)
            primary = str(probe["queries"][0])
            row = c.execute("SELECT id FROM sources WHERE name=?", (probe["name"],)).fetchone()
            if row:
                c.execute(
                    """UPDATE sources SET source_type='agent_reach',platform=?,query=?,url=NULL,config_json=?,
                       enabled=0,updated_at=? WHERE id=?""",
                    (probe["platform"], primary, dumps(cfg), now, int(row["id"])),
                )
                updated += 1
            else:
                c.execute(
                    """INSERT INTO sources(name,source_type,platform,query,url,config_json,enabled,status,created_at,updated_at)
                       VALUES(?,?,?,?,NULL,?,0,'NOT_CHECKED',?,?)""",
                    (probe["name"], "agent_reach", probe["platform"], primary, dumps(cfg), now, now),
                )
                created += 1
    return {"created": created, "updated": updated, "retired_legacy": retired}


def _channel_ready(doctor: dict[str, Any], platform: str) -> bool:
    opencli = doctor.get("opencli") if isinstance(doctor, dict) else None
    if isinstance(opencli, dict) and opencli.get("connected"):
        return True
    channels = doctor.get("channels") if isinstance(doctor, dict) else None
    info = channels.get(platform) if isinstance(channels, dict) else None
    if not isinstance(info, dict):
        return False
    status = str(info.get("effective_status") or info.get("status") or "").lower()
    backend = str(info.get("effective_backend") or info.get("active_backend") or "").lower()
    return status in {"ok", "ready", "connected"} or "opencli" in backend


def _source_for_probe(probe: dict[str, Any]) -> dict[str, Any]:
    with conn() as c:
        row = c.execute("SELECT * FROM sources WHERE name=?", (probe["name"],)).fetchone()
    if not row:
        raise RuntimeError(f"managed demand source missing: {probe['name']}")
    return row_to_dict(row) or {}


def _intent_counts(evidence_ids: list[int]) -> dict[str, int]:
    if not evidence_ids:
        return {"explicit_demand": 0, "probable_demand": 0, "discussion": 0, "supply": 0, "showcase": 0, "noise": 0}
    placeholders = ",".join("?" for _ in evidence_ids)
    with conn() as c:
        rows = c.execute(
            f"SELECT intent_class,COUNT(*) c FROM evidence WHERE id IN ({placeholders}) GROUP BY intent_class",
            tuple(evidence_ids),
        ).fetchall()
    result = {"explicit_demand": 0, "probable_demand": 0, "discussion": 0, "supply": 0, "showcase": 0, "noise": 0}
    for row in rows:
        result[str(row["intent_class"])] = int(row["c"])
    return result


def _collect_probe(
    probe: dict[str, Any],
    *,
    queries: tuple[str, ...] | list[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    source = _source_for_probe(probe)
    started = now_iso()
    t0 = time.monotonic()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO collection_runs(source_id,status,started_at) VALUES(?,?,?)",
            (int(source["id"]), "RUNNING", started),
        )
        run_id = int(cur.lastrowid)

    raw_items: list[CollectedItem] = []
    query_errors: list[str] = []
    selected_queries = list(queries if queries is not None else probe["queries"])
    for query_index, query in enumerate(selected_queries, start=1):
        query_started = time.monotonic()
        fetched = 0
        error: str | None = None
        try:
            items = collect_agent_reach(
                str(probe["platform"]),
                str(query),
                limit=int(probe["per_query_limit"]),
                enrich=False,
            )
            fetched = len(items)
            raw_items.extend(items)
        except Exception as exc:
            error = str(exc)
            query_errors.append(f"{query}: {error}")
        finally:
            if progress_callback is not None:
                progress_callback({
                    "source_name": probe["name"],
                    "platform": probe["platform"],
                    "query": str(query),
                    "query_index": query_index,
                    "query_total": len(selected_queries),
                    "fetched": fetched,
                    "error": error,
                    "duration_ms": int((time.monotonic() - query_started) * 1000),
                })

    retained = _prepare_items(raw_items, int(probe["limit"]))
    stats = ingest_items(source, retained)
    observed_ids = [int(x) for x in stats.get("observed_evidence_ids") or []]
    intents = _intent_counts(observed_ids)
    unique_authors = len({str(item.author).strip().lower() for item in retained if item.author and str(item.author).strip()})
    duration_ms = int((time.monotonic() - t0) * 1000)
    all_queries_failed = bool(selected_queries) and len(query_errors) == len(selected_queries)
    if retained:
        status = "SUCCESS"
    elif all_queries_failed:
        status = "FAILED"
    else:
        status = "EMPTY"
    error = " | ".join(query_errors[:4]) if query_errors else None
    source_status = "READY" if retained else ("ERROR" if status == "FAILED" else "EMPTY")
    now = now_iso()
    with conn() as c:
        c.execute(
            """UPDATE collection_runs SET status=?,finished_at=?,fetched_count=?,inserted_count=?,duplicate_count=?,
               rejected_count=?,duration_ms=?,error=? WHERE id=?""",
            (
                status, now, len(raw_items), int(stats.get("inserted") or 0), int(stats.get("duplicates") or 0),
                max(0, len(raw_items) - len(retained)), duration_ms, error, run_id,
            ),
        )
        c.execute(
            """UPDATE sources SET status=?,last_error=?,last_checked_at=?,last_success_at=?,last_item_count=?,
               failure_streak=?,empty_streak=?,next_retry_at=NULL,avg_duration_ms=?,updated_at=? WHERE id=?""",
            (
                source_status, error, now, now if retained else source.get("last_success_at"),
                len(retained),
                int(source.get("failure_streak") or 0) + 1 if status == "FAILED" else 0,
                int(source.get("empty_streak") or 0) + 1 if status == "EMPTY" else 0,
                duration_ms, now, int(source["id"]),
            ),
        )
    return {
        "source_id": int(source["id"]),
        "source_name": probe["name"],
        "platform": probe["platform"],
        "status": status,
        "queries": len(selected_queries),
        "query_failures": len(query_errors),
        "raw_fetched": len(raw_items),
        "retained_recent": len(retained),
        "unique_authors": unique_authors,
        "inserted": int(stats.get("inserted") or 0),
        "duplicates": int(stats.get("duplicates") or 0),
        "intent_counts": intents,
        "duration_ms": duration_ms,
        "errors": query_errors[:4],
    }


def run_demand_discovery(
    *,
    max_queries_per_probe: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Refresh current high-intent demand before opportunity clustering/Agent analysis.

    ``max_queries_per_probe`` bounds interactive rebuild latency without fabricating
    data. Pass ``None`` to run the complete query matrix. Progress is emitted after
    each real query so callers can distinguish slow collection from a stuck worker.
    """
    install = install_sources()
    doctor = agent_reach_doctor()
    ready_platforms = {platform for platform in {"xiaohongshu", "reddit"} if _channel_ready(doctor, platform)}
    if not ready_platforms:
        raise RuntimeError("fresh demand discovery unavailable: Xiaohongshu/Reddit OpenCLI channels are not ready")

    probes = [probe for probe in _PROBES if probe["platform"] in ready_platforms]
    if max_queries_per_probe is not None:
        max_queries_per_probe = max(1, int(max_queries_per_probe))
    query_plan = {
        str(probe["name"]): tuple(probe["queries"][:max_queries_per_probe]) if max_queries_per_probe is not None else tuple(probe["queries"])
        for probe in probes
    }
    total_queries = sum(len(queries) for queries in query_plan.values())
    completed_queries = 0
    progress_lock = threading.Lock()

    def emit_query_progress(event: dict[str, Any]) -> None:
        nonlocal completed_queries
        with progress_lock:
            completed_queries += 1
            payload = dict(event)
            payload["completed_queries"] = completed_queries
            payload["total_queries"] = total_queries
        if progress_callback is not None:
            progress_callback(payload)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(probes))), thread_name_prefix="demand-discovery") as pool:
        futures = {
            pool.submit(
                _collect_probe,
                probe,
                queries=query_plan[str(probe["name"])],
                progress_callback=emit_query_progress,
            ): probe
            for probe in probes
        }
        for future in as_completed(futures):
            probe = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "source_name": probe["name"],
                    "platform": probe["platform"],
                    "status": "FAILED",
                    "error": str(exc),
                })

    successful = [x for x in results if x.get("status") in {"SUCCESS", "EMPTY"}]
    direct_signals = sum(
        int((x.get("intent_counts") or {}).get("explicit_demand") or 0)
        + int((x.get("intent_counts") or {}).get("probable_demand") or 0)
        for x in results
    )
    retained = sum(int(x.get("retained_recent") or 0) for x in results)
    authors = sum(int(x.get("unique_authors") or 0) for x in results)
    if not successful:
        errors = "; ".join(str(x.get("error") or "collection failed") for x in results)
        raise RuntimeError(f"fresh demand discovery failed: {errors}")

    return {
        "status": "SUCCESS" if len(successful) == len(results) else "PARTIAL",
        "strategy": STRATEGY_VERSION,
        "max_age_days": MAX_AGE_DAYS,
        "max_queries_per_probe": max_queries_per_probe,
        "total_queries": total_queries,
        "ready_platforms": sorted(ready_platforms),
        "probes": results,
        "probe_count": len(results),
        "successful_probes": len(successful),
        "retained_recent": retained,
        "independent_author_slots": authors,
        "direct_demand_signals": direct_signals,
        "source_install": install,
        "generated_at": now_iso(),
    }
