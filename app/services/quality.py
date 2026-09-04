from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings
from ..db import conn, loads, now_iso, row_to_dict


def data_quality_report() -> dict[str, Any]:
    with conn() as c:
        evidence_rows = c.execute(
            """SELECT id,platform,published_at,collected_at,author,content_fingerprint,evidence_quality_score,
                      published_time_quality,demand_signal_score,intent_class,intent_confidence,content_kind FROM evidence"""
        ).fetchall()
        link_rows = c.execute(
            """SELECT es.evidence_id,s.id source_id,s.name,s.platform,s.config_json
               FROM evidence_sources es JOIN sources s ON s.id=es.source_id"""
        ).fetchall()
        duplicate_rows = c.execute(
            """SELECT content_fingerprint,COUNT(*) c,COUNT(DISTINCT platform) platforms
               FROM evidence WHERE content_fingerprint IS NOT NULL AND content_fingerprint<>''
               GROUP BY content_fingerprint HAVING COUNT(*)>1 ORDER BY c DESC LIMIT 20"""
        ).fetchall()
        run_rows = c.execute(
            """SELECT cr.*,s.name source_name,s.platform FROM collection_runs cr
               JOIN sources s ON s.id=cr.source_id ORDER BY cr.id DESC LIMIT 500"""
        ).fetchall()
        source_rows = c.execute("SELECT * FROM sources ORDER BY id").fetchall()

    roles_by_e: dict[int, set[str]] = {}
    for r in link_rows:
        cfg = loads(r["config_json"], {}) or {}
        roles_by_e.setdefault(int(r["evidence_id"]), set()).add(str(cfg.get("role") or "demand"))

    total = len(evidence_rows)
    role_counts = Counter()
    platform_counts = Counter()
    source_time_count = 0
    authored = 0
    quality_sum = 0.0
    demand_signal_sum = 0.0
    demand_signal_n = 0
    fingerprints: set[str] = set()
    intent_counts = Counter()
    content_kind_counts = Counter()
    demand_eligible = 0
    for r in evidence_rows:
        platform_counts[str(r["platform"])] += 1
        intent_counts[str(r["intent_class"] or "unclassified")] += 1
        content_kind_counts[str(r["content_kind"] or "post")] += 1
        if str(r["intent_class"] or "") in {"explicit_demand", "probable_demand"}:
            demand_eligible += 1
        for role in roles_by_e.get(int(r["id"]), {"demand"}):
            role_counts[role] += 1
        if r["published_time_quality"] == "source":
            source_time_count += 1
        if r["author"]:
            authored += 1
        quality_sum += float(r["evidence_quality_score"] or 0)
        if "demand" in roles_by_e.get(int(r["id"]), {"demand"}):
            demand_signal_sum += float(r["demand_signal_score"] or 0)
            demand_signal_n += 1
        if r["content_fingerprint"]:
            fingerprints.add(str(r["content_fingerprint"]))

    per_source_runs: dict[int, list[dict[str, Any]]] = {}
    for r in run_rows:
        per_source_runs.setdefault(int(r["source_id"]), []).append(row_to_dict(r) or {})
    source_reliability = []
    for srow in source_rows:
        s = row_to_dict(srow) or {}
        runs = per_source_runs.get(int(s["id"]), [])[:20]
        completed = [r for r in runs if r.get("status") != "RUNNING"]
        good = [r for r in completed if r.get("status") in {"SUCCESS", "EMPTY"}]
        fail = [r for r in completed if r.get("status") == "FAILED"]
        empty = [r for r in completed if r.get("status") == "EMPTY"]
        source_reliability.append({
            "source_id": s["id"],
            "name": s["name"],
            "platform": s["platform"],
            "status": s["status"],
            "sample_runs": len(completed),
            "success_rate_pct": round(len(good) / len(completed) * 100.0, 2) if completed else None,
            "failure_rate_pct": round(len(fail) / len(completed) * 100.0, 2) if completed else None,
            "empty_rate_pct": round(len(empty) / len(completed) * 100.0, 2) if completed else None,
            "avg_duration_ms": round(float(s.get("avg_duration_ms") or 0), 2),
            "failure_streak": int(s.get("failure_streak") or 0),
            "empty_streak": int(s.get("empty_streak") or 0),
            "next_retry_at": s.get("next_retry_at"),
            "last_success_at": s.get("last_success_at"),
        })

    now = datetime.now(timezone.utc)
    stale_cut = (now - timedelta(hours=max(1, settings.source_stale_after_hours))).isoformat(timespec="seconds")
    stale_sources = sum(1 for s in source_rows if s["enabled"] and (not s["last_success_at"] or str(s["last_success_at"]) < stale_cut))
    duplicated_rows = sum(max(0, int(r["c"]) - 1) for r in duplicate_rows)
    return {
        "generated_at": now_iso(),
        "real_data_only": True,
        "evidence_total": total,
        "independent_content_fingerprints": len(fingerprints),
        "mirrored_or_exact_copy_extra_rows": duplicated_rows,
        "published_timestamp_coverage_pct": round(source_time_count / total * 100.0, 2) if total else 0.0,
        "author_coverage_pct": round(authored / total * 100.0, 2) if total else 0.0,
        "avg_evidence_quality_score": round(quality_sum / total, 2) if total else 0.0,
        "avg_demand_signal_score": round(demand_signal_sum / demand_signal_n, 2) if demand_signal_n else 0.0,
        "role_counts": dict(role_counts),
        "intent_counts": dict(intent_counts),
        "content_kind_counts": dict(content_kind_counts),
        "demand_eligible_evidence": demand_eligible,
        "platform_counts": dict(platform_counts),
        "stale_or_never_successful_sources": stale_sources,
        "source_stale_after_hours": settings.source_stale_after_hours,
        "duplicate_groups": [row_to_dict(r) for r in duplicate_rows],
        "source_reliability": source_reliability,
        "notes": [
            "duplicate_groups are retained as provenance but de-inflated in effective demand signal counts",
            "evidence quality is a transparent completeness score, not an AI judgment",
            "only explicit_demand/probable_demand from core_demand sources can enter business demand clustering",
            "source reliability uses the latest 20 completed collection runs per source",
        ],
    }
