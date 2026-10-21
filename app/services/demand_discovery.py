from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..collectors import agent_reach_doctor, collect_agent_reach
from ..collectors.base import CollectedItem
from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict
from ..evidence_identity import account_identity
from ..security import redact_error
from .ingest import ingest_items
from .demand_topics import DEMAND_TOPICS, detect_topic, topic_matches_text
from .opportunities import get_evidence_for_opportunity, get_opportunity

STRATEGY_VERSION = "demand-discovery-v5-topic-provenance"
MAX_AGE_DAYS = 30
MAX_WORKERS = 2

# V4 keeps real-demand language as the hard requirement, but corroborates it around
# digital goods this project can actually build and deliver (xlsx/csv/scripts/text/web).
# V3's generic "template/tool/help" probes produced many unrelated one-off requests:
# real signals existed, but almost every signal formed a singleton cluster and could
# never satisfy independent-demand validation. These queries remain demand-first;
# they do not treat marketplace supply as demand and do not fabricate corroboration.
_PROBES: tuple[dict[str, Any], ...] = (
    {
        "name": "小红书 - 直接需求扫描 v3",
        "platform": "xiaohongshu",
        "limit": 60,
        "per_query_limit": 12,
        "queries": (
            "求 Excel 模板",
            "求 表格 模板",
            "有没有 自动统计 表格",
            "求 记账 表格",
            "求 库存 表格",
            "求 排班 表格",
            "有没有 批量处理 工具",
            "求 自动化 脚本",
        ),
    },
    {
        "name": "小红书 - 付费意图扫描 v3",
        "platform": "xiaohongshu",
        "limit": 50,
        "per_query_limit": 10,
        "queries": (
            "付费 求 Excel 模板",
            "愿意付费 表格 模板",
            "预算 自动化 脚本",
            "哪里买 Excel 模板",
            "求定制 Excel 表格",
            "付费 求 自动化 工具",
            "小店 记账 表格 求",
            "库存 表格 求推荐",
        ),
    },
    {
        "name": "Reddit - direct demand scan v3",
        "platform": "reddit",
        "limit": 60,
        "per_query_limit": 12,
        "queries": (
            "need spreadsheet template",
            "looking for excel template",
            "need inventory spreadsheet",
            "need bookkeeping spreadsheet",
            "need scheduling spreadsheet",
            "looking for automation script",
            "need a tool to automate spreadsheet",
            "small business spreadsheet template",
        ),
    },
    {
        "name": "Reddit - purchase intent scan v3",
        "platform": "reddit",
        "limit": 50,
        "per_query_limit": 10,
        "queries": (
            "paid spreadsheet template",
            "where can I buy spreadsheet template",
            "willing to pay for excel template",
            "budget for automation script",
            "paid automation tool recommendation",
            "would pay for spreadsheet automation",
            "buy small business spreadsheet template",
            "paid inventory spreadsheet",
        ),
    },
)

_LEGACY_NARROW_P0 = {
    "小红书 - 求Excel模板", "小红书 - 求效率工具", "小红书 - 求职简历痛点",
    "小红书 - 店铺经营表格", "小红书 - 短视频运营痛点",
    "Reddit - productivity template demand", "Reddit - small business spreadsheet demand",
    "Reddit - creator workflow demand",
}


_CORROBORATION_SOURCES: tuple[dict[str, str], ...] = (
    {"name": "小红书 - 动态同主题佐证 v4", "platform": "xiaohongshu"},
    {"name": "Reddit - dynamic corroboration v4", "platform": "reddit"},
)

# Corroboration topics live in demand_topics.py so discovery and clustering use the
# exact same deterministic taxonomy. Broad "automation"/"spreadsheet" buckets are
# intentionally excluded because they created false commercial corroboration.
_CORROBORATION_TOPICS = DEMAND_TOPICS
_MAX_CORROBORATION_TOPICS = 5


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
        return item.platform, f"{item.content_kind}:id:{item.source_item_id}"
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
        author, _ = account_identity(item.platform, item.author, item.raw)
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
        "enrich": True,
        "detail_limit": max(0, min(5, settings.demand_detail_limit)),
        "comments_limit": max(0, min(50, settings.demand_comments_limit)),
        "query_variants": list(probe["queries"]),
    }


def install_sources() -> dict[str, int]:
    """Install managed high-intent demand probes and retire the narrow v2 probes.

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

        for managed in _CORROBORATION_SOURCES:
            cfg = {
                "role": "demand",
                "priority": "P0",
                "business_group": "core_demand",
                "recommended": True,
                "managed_refresh": True,
                "strategy_version": STRATEGY_VERSION,
                "max_age_days": MAX_AGE_DAYS,
                "mode": "dynamic_corroboration",
            }
            row = c.execute("SELECT id FROM sources WHERE name=?", (managed["name"],)).fetchone()
            if row:
                c.execute(
                    """UPDATE sources SET source_type='agent_reach',platform=?,query='dynamic',url=NULL,config_json=?,
                       enabled=0,updated_at=? WHERE id=?""",
                    (managed["platform"], dumps(cfg), now, int(row["id"])),
                )
                updated += 1
            else:
                c.execute(
                    """INSERT INTO sources(name,source_type,platform,query,url,config_json,enabled,status,created_at,updated_at)
                       VALUES(?,?,?,'dynamic',NULL,?,0,'NOT_CHECKED',?,?)""",
                    (managed["name"], "agent_reach", managed["platform"], dumps(cfg), now, now),
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


def _collect_with_details(platform: str, query: str, limit: int) -> list[CollectedItem]:
    return collect_agent_reach(platform, query, limit=limit, enrich=True,
                               detail_limit=max(0, min(5, settings.demand_detail_limit)),
                               comments_limit=max(0, min(50, settings.demand_comments_limit)))


def _enrichment_failures(items: list[CollectedItem]) -> list[str]:
    failures = []
    for item in items:
        state = item.raw.get("_enrichment") or {}
        for stage in ("detail", "comments"):
            if state.get(stage + "_status") in {"FAILED", "NOT_AVAILABLE"}:
                failures.append(f"{item.platform}:{item.source_item_id}:{stage}: " + redact_error(
                    state.get(stage + "_error") or state.get("error") or state[stage + "_status"]))
    return failures


_PLATFORM_PARSE_FAILURE_THRESHOLD = 2


def _is_platform_parse_failure(error: str | None) -> bool:
    text = str(error or "").lower()
    return "no parseable real items" in text


def _platform_circuit_open(
    state: dict[str, Any] | None,
    platform: str,
) -> tuple[bool, str | None]:
    if not state:
        return False, None
    lock = state["lock"]
    with lock:
        reason = (state.get("disabled") or {}).get(platform)
    return bool(reason), str(reason) if reason else None


def _record_platform_query_result(
    state: dict[str, Any] | None,
    platform: str,
    error: str | None,
) -> None:
    if not state:
        return
    lock = state["lock"]
    with lock:
        failures = state.setdefault("parse_failures", {})
        disabled = state.setdefault("disabled", {})
        if _is_platform_parse_failure(error):
            count = int(failures.get(platform) or 0) + 1
            failures[platform] = count
            if count >= _PLATFORM_PARSE_FAILURE_THRESHOLD and platform not in disabled:
                disabled[platform] = (
                    f"{platform} disabled for this discovery run after {count} consecutive "
                    "OpenCLI parse failures"
                )
        elif error is None:
            failures[platform] = 0


def _collect_probe(
    probe: dict[str, Any],
    *,
    queries: tuple[str, ...] | list[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    platform_circuit: dict[str, Any] | None = None,
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
    attempted_queries = 0
    circuit_reason: str | None = None
    for query_index, query in enumerate(selected_queries, start=1):
        platform = str(probe["platform"])
        is_open, circuit_reason = _platform_circuit_open(platform_circuit, platform)
        if is_open:
            break

        attempted_queries += 1
        query_started = time.monotonic()
        fetched = 0
        error: str | None = None
        try:
            items = _collect_with_details(
                platform,
                str(query),
                limit=int(probe["per_query_limit"]),
            )
            fetched = len(items)
            raw_items.extend(items)
        except Exception as exc:
            error = redact_error(exc)
            query_errors.append(f"{query}: {error}")
        finally:
            _record_platform_query_result(platform_circuit, platform, error)
            if progress_callback is not None:
                progress_callback({
                    "source_name": probe["name"],
                    "platform": platform,
                    "query": str(query),
                    "query_index": query_index,
                    "query_total": len(selected_queries),
                    "fetched": fetched,
                    "error": error,
                    "duration_ms": int((time.monotonic() - query_started) * 1000),
                })

        is_open, circuit_reason = _platform_circuit_open(platform_circuit, platform)
        if is_open:
            break

    retained = _prepare_items(raw_items, int(probe["limit"]))
    stats = ingest_items(source, retained)
    observed_ids = [int(x) for x in stats.get("observed_evidence_ids") or []]
    intents = _intent_counts(observed_ids)
    unique_authors = len({str(item.author).strip().lower() for item in retained if item.author and str(item.author).strip()})
    duration_ms = int((time.monotonic() - t0) * 1000)
    all_queries_failed = bool(attempted_queries) and len(query_errors) == attempted_queries
    enrichment_errors = _enrichment_failures(raw_items)
    if retained:
        status = "PARTIAL" if query_errors or enrichment_errors or circuit_reason else "SUCCESS"
    elif all_queries_failed:
        status = "FAILED"
    elif query_errors or enrichment_errors or circuit_reason:
        status = "PARTIAL"
    else:
        status = "EMPTY"
    error = " | ".join((query_errors + enrichment_errors)[:4]) or None
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
        "queries": attempted_queries,
        "queries_planned": len(selected_queries),
        "query_failures": len(query_errors),
        "circuit_open": bool(circuit_reason),
        "circuit_reason": circuit_reason,
        "raw_fetched": len(raw_items),
        "retained_recent": len(retained),
        "unique_authors": unique_authors,
        "inserted": int(stats.get("inserted") or 0),
        "duplicates": int(stats.get("duplicates") or 0),
        "intent_counts": intents,
        "observed_evidence_ids": sorted(set(observed_ids)),
        "enrichment_failures": len(enrichment_errors),
        "enrichment_errors": enrichment_errors[:4],
        "duration_ms": duration_ms,
        "errors": query_errors[:4],
    }


def _corroboration_source(platform: str) -> dict[str, Any]:
    managed = next((item for item in _CORROBORATION_SOURCES if item["platform"] == platform), None)
    if not managed:
        raise RuntimeError(f"no corroboration source for {platform}")
    with conn() as c:
        row = c.execute("SELECT * FROM sources WHERE name=?", (managed["name"],)).fetchone()
    if not row:
        raise RuntimeError(f"managed corroboration source missing: {managed['name']}")
    return row_to_dict(row) or {}


def _topic_for_text(title: str | None, content: str | None) -> dict[str, Any] | None:
    return detect_topic(title, content)


def _recent_direct_topics(limit: int = 120) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).isoformat(timespec="seconds")
    with conn() as c:
        source_rows = c.execute("SELECT id,config_json FROM sources").fetchall()
        eligible_source_ids = []
        for row in source_rows:
            cfg = loads(row["config_json"], {}) or {}
            if str(cfg.get("role") or "demand").lower() != "demand":
                continue
            if str(cfg.get("business_group") or "core_demand") != "core_demand":
                continue
            eligible_source_ids.append(int(row["id"]))
        if not eligible_source_ids:
            return []
        placeholders = ",".join("?" for _ in eligible_source_ids)
        rows = c.execute(
            f"""SELECT DISTINCT e.id,e.platform,e.title,e.content,e.author,e.intent_class,
                       e.evidence_quality_score,e.demand_signal_score,e.collected_at,e.published_at,e.raw_json
                FROM evidence e
                JOIN evidence_sources es ON es.evidence_id=e.id
                WHERE es.source_id IN ({placeholders})
                  AND e.intent_class IN ('explicit_demand','probable_demand')
                  AND e.collected_at>=?
                ORDER BY e.demand_signal_score DESC,e.evidence_quality_score DESC,e.id DESC
                LIMIT ?""",
            (*eligible_source_ids, cutoff, max(1, min(int(limit), 500))),
        ).fetchall()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = row_to_dict(row) or {}
        published = _parse_dt(data.get("published_at"))
        if not published or not datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS) <= published <= datetime.now(timezone.utc):
            continue
        topic = _topic_for_text(data.get("title"), data.get("content"))
        if not topic:
            continue
        key = str(topic["key"])
        bucket = grouped.setdefault(
            key,
            {
                "key": key,
                "topic": topic,
                "evidence_ids": set(),
                "authors": set(),
                "platforms": set(),
                "signal_score": 0.0,
            },
        )
        bucket["evidence_ids"].add(int(data["id"]))
        author, _ = account_identity(str(data.get("platform") or ""), data.get("author"), loads(data.get("raw_json"), {}) or {})
        if author:
            bucket["authors"].add(author)
        platform = str(data.get("platform") or "").strip().lower()
        if platform:
            bucket["platforms"].add(platform)
        bucket["signal_score"] = max(bucket["signal_score"], float(data.get("demand_signal_score") or 0))

    values = list(grouped.values())
    values.sort(
        key=lambda item: (
            len(item["authors"]),
            len(item["platforms"]),
            -float(item["signal_score"]),
            item["key"],
        )
    )
    for item in values:
        item["evidence_ids"] = sorted(item["evidence_ids"])
        item["authors"] = sorted(item["authors"])
        item["platforms"] = sorted(item["platforms"])
    return values[:_MAX_CORROBORATION_TOPICS]


def _item_matches_topic(item: CollectedItem, topic: dict[str, Any]) -> bool:
    text = f"{item.title or ''} {item.content or ''}".lower()
    return topic_matches_text(text, topic)


def _tag_corroboration_item(item: CollectedItem, topic_key: str, query: str) -> CollectedItem:
    # This is provenance metadata about why the real item was retained. It does not
    # alter its title/content/author/intent and therefore cannot fabricate demand.
    raw = dict(item.raw or {})
    raw["_corroboration_topic"] = topic_key
    raw["_corroboration_query"] = query
    item.raw = raw
    return item


def _run_dynamic_corroboration(
    ready_platforms: set[str],
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    topics = _recent_direct_topics()
    plan: list[tuple[dict[str, Any], str, str]] = []
    for item in topics:
        topic = item["topic"]
        for platform in sorted(ready_platforms):
            for query in topic["queries"].get(platform, ()):
                plan.append((item, platform, str(query)))

    results: list[dict[str, Any]] = []
    observed_ids: set[int] = set()
    for index, (topic_info, platform, query) in enumerate(plan, start=1):
        fetched = retained_count = 0
        error: str | None = None
        intent_counts = _intent_counts([])
        try:
            items = _collect_with_details(platform, query, limit=10)
            error = " | ".join(_enrichment_failures(items)[:4]) or None
            fetched = len(items)
            matching = [item for item in items if _item_matches_topic(item, topic_info["topic"])]
            retained = _prepare_items(matching, 8)
            retained = [
                _tag_corroboration_item(item, str(topic_info["key"]), query)
                for item in retained
            ]
            retained_count = len(retained)
            stats = ingest_items(_corroboration_source(platform), retained)
            ids = [int(value) for value in stats.get("observed_evidence_ids") or []]
            observed_ids.update(ids)
            intent_counts = _intent_counts(ids)
        except Exception as exc:
            error = str(exc)
        result = {
            "topic": topic_info["key"],
            "platform": platform,
            "query": query,
            "fetched": fetched,
            "retained_recent": retained_count,
            "intent_counts": intent_counts,
            "error": error,
        }
        results.append(result)
        if progress_callback is not None:
            progress_callback({
                **result,
                "phase": "corroboration",
                "corroboration_index": index,
                "corroboration_total": len(plan),
            })

    direct = _intent_counts(sorted(observed_ids))
    return {
        "topics": [
            {
                "key": item["key"],
                "seed_evidence_ids": item["evidence_ids"],
                "seed_authors": item["authors"],
                "seed_platforms": item["platforms"],
            }
            for item in topics
        ],
        "queries": len(plan),
        "results": results,
        "observed_evidence_ids": sorted(observed_ids),
        "direct_demand_signals": int(direct.get("explicit_demand") or 0) + int(direct.get("probable_demand") or 0),
    }



def _opportunity_corroboration_topic(opportunity_id: int) -> dict[str, Any]:
    opportunity = get_opportunity(opportunity_id)
    if not opportunity:
        raise KeyError("opportunity not found")

    evidence = get_evidence_for_opportunity(opportunity_id, 120)
    demand_rows = [
        item for item in evidence
        if str(item.get("relation_type") or "").lower() == "demand"
        and str(item.get("intent_class") or "").lower() in {"explicit_demand", "probable_demand"}
    ]
    topics: dict[str, dict[str, Any]] = {}
    for item in demand_rows:
        topic = detect_topic(item.get("title"), item.get("content"))
        if not topic:
            continue
        key = str(topic["key"])
        bucket = topics.setdefault(
            key,
            {
                "key": key,
                "topic": topic,
                "evidence_ids": [],
                "authors": set(),
                "platforms": set(),
            },
        )
        bucket["evidence_ids"].append(int(item["id"]))
        author, _ = account_identity(str(item.get("platform") or ""), item.get("author"), item.get("raw") or {})
        platform = str(item.get("platform") or "").strip().lower()
        if author:
            bucket["authors"].add(author)
        if platform:
            bucket["platforms"].add(platform)

    if not topics:
        fallback = detect_topic(
            opportunity.get("title"),
            opportunity.get("normalized_need"),
        )
        if not fallback:
            raise ValueError("WATCH opportunity has no deterministic corroboration topic")
        topics[str(fallback["key"])] = {
            "key": str(fallback["key"]),
            "topic": fallback,
            "evidence_ids": [int(item["id"]) for item in demand_rows if item.get("id") is not None],
            "authors": {
                account_identity(str(item.get("platform") or ""), item.get("author"), item.get("raw") or {})[0]
                for item in demand_rows
                if str(item.get("author") or "").strip()
            },
            "platforms": {
                str(item.get("platform") or "").strip().lower()
                for item in demand_rows
                if str(item.get("platform") or "").strip()
            },
        }

    if len(topics) != 1:
        raise ValueError(
            "WATCH opportunity contains multiple deterministic demand topics; "
            "split/rebuild clustering before targeted corroboration: "
            + ",".join(sorted(topics))
        )

    result = next(iter(topics.values()))
    result["evidence_ids"] = sorted(set(result["evidence_ids"]))
    result["authors"] = sorted(author for author in result["authors"] if author)
    result["platforms"] = sorted(result["platforms"])
    return result


def refresh_opportunity_corroboration(
    opportunity_id: int,
    *,
    per_platform_limit: int = 12,
) -> dict[str, Any]:
    """Collect real same-topic demand for one WATCH opportunity.

    New hits are ingested through the normal evidence pipeline. They are not attached
    directly to the opportunity; clustering must decide whether they belong together.
    """
    install_sources()
    topic_info = _opportunity_corroboration_topic(opportunity_id)
    doctor = agent_reach_doctor()
    ready_platforms = {
        platform
        for platform in {"xiaohongshu", "reddit"}
        if _channel_ready(doctor, platform)
    }
    if not ready_platforms:
        raise RuntimeError(
            "targeted corroboration unavailable: Xiaohongshu/Reddit OpenCLI channels are not ready"
        )

    limit = max(1, min(int(per_platform_limit), 20))
    results: list[dict[str, Any]] = []
    observed_ids: set[int] = set()
    seed_authors = set(topic_info["authors"])

    for platform in sorted(ready_platforms):
        queries = tuple(topic_info["topic"]["queries"].get(platform, ()))
        for query in queries:
            fetched = 0
            retained_count = 0
            error: str | None = None
            intent_counts = _intent_counts([])
            try:
                items = _collect_with_details(platform, str(query), limit=limit)
                error = " | ".join(_enrichment_failures(items)[:4]) or None
                fetched = len(items)
                matching = [
                    item for item in items
                    if _item_matches_topic(item, topic_info["topic"])
                ]
                retained = _prepare_items(matching, limit)
                retained = [
                    _tag_corroboration_item(item, str(topic_info["key"]), str(query))
                    for item in retained
                ]
                retained_count = len(retained)
                stats = ingest_items(_corroboration_source(platform), retained)
                ids = [int(value) for value in stats.get("observed_evidence_ids") or []]
                observed_ids.update(ids)
                intent_counts = _intent_counts(ids)
            except Exception as exc:
                error = str(exc)

            results.append({
                "topic": topic_info["key"],
                "platform": platform,
                "query": str(query),
                "fetched": fetched,
                "retained_recent": retained_count,
                "intent_counts": intent_counts,
                "error": error,
            })

    observed_author_slots: set[str] = set()
    if observed_ids:
        placeholders = ",".join("?" for _ in observed_ids)
        with conn() as c:
            rows = c.execute(
                f"SELECT platform,author,raw_json FROM evidence WHERE id IN ({placeholders})",
                tuple(sorted(observed_ids)),
            ).fetchall()
        for row in rows:
            author, _ = account_identity(row["platform"], row["author"], loads(row["raw_json"], {}) or {})
            if author:
                observed_author_slots.add(author)

    intents = _intent_counts(sorted(observed_ids))
    return {
        "opportunity_id": opportunity_id,
        "mode": "watch-targeted-corroboration-v1",
        "topic": topic_info["key"],
        "seed_evidence_ids": topic_info["evidence_ids"],
        "seed_authors": sorted(seed_authors),
        "seed_platforms": topic_info["platforms"],
        "ready_platforms": sorted(ready_platforms),
        "queries": len(results),
        "results": results,
        "observed_evidence_ids": sorted(observed_ids),
        "new_author_slots": sorted(observed_author_slots - seed_authors),
        "direct_demand_signals": int(intents.get("explicit_demand") or 0)
        + int(intents.get("probable_demand") or 0),
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

    platform_circuit: dict[str, Any] = {
        "lock": threading.Lock(),
        "parse_failures": {},
        "disabled": {},
    }
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(probes))), thread_name_prefix="demand-discovery") as pool:
        futures = {
            pool.submit(
                _collect_probe,
                probe,
                queries=query_plan[str(probe["name"])],
                progress_callback=emit_query_progress,
                platform_circuit=platform_circuit,
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
    usable = [x for x in results if x.get("status") in {"SUCCESS", "EMPTY", "PARTIAL"}]
    if not usable:
        errors = "; ".join(str(x.get("error") or "collection failed") for x in results)
        raise RuntimeError(f"fresh demand discovery failed: {errors}")

    with platform_circuit["lock"]:
        disabled_platforms = dict(platform_circuit.get("disabled") or {})
    corroboration_platforms = set(ready_platforms) - set(disabled_platforms)
    corroboration = _run_dynamic_corroboration(
        corroboration_platforms,
        progress_callback=progress_callback,
    )
    observed_ids = {int(eid) for result in results for eid in result.get("observed_evidence_ids", [])}
    observed_ids.update(int(eid) for eid in corroboration.get("observed_evidence_ids", []))
    intents = _intent_counts(sorted(observed_ids))
    accounts = set()
    if observed_ids:
        with conn() as c:
            rows = c.execute("SELECT platform,author,raw_json FROM evidence WHERE id IN (" + ",".join("?" for _ in observed_ids) + ")", tuple(sorted(observed_ids))).fetchall()
        for row in rows:
            account, _ = account_identity(row["platform"], row["author"], loads(row["raw_json"], {}) or {})
            if account:
                accounts.add(account)
    corroboration_failed = any(row.get("error") for row in corroboration.get("results", []))

    return {
        "status": "SUCCESS" if len(successful) == len(results) and not corroboration_failed else "PARTIAL",
        "strategy": STRATEGY_VERSION,
        "max_age_days": MAX_AGE_DAYS,
        "max_queries_per_probe": max_queries_per_probe,
        "total_queries": total_queries + int(corroboration.get("queries") or 0),
        "ready_platforms": sorted(ready_platforms),
        "corroboration_platforms": sorted(corroboration_platforms),
        "disabled_platforms_this_run": disabled_platforms,
        "probes": results,
        "probe_count": len(results),
        "successful_probes": len(successful),
        "retained_recent": len(observed_ids),
        "retained_occurrences": sum(int(x.get("retained_recent") or 0) for x in results) + sum(int(x.get("retained_recent") or 0) for x in corroboration.get("results", [])),
        "observed_evidence_ids": sorted(observed_ids),
        "independent_author_slots": len(accounts),
        "direct_demand_signals": int(intents.get("explicit_demand", 0)) + int(intents.get("probable_demand", 0)),
        "corroboration": corroboration,
        "source_install": install,
        "generated_at": now_iso(),
    }
