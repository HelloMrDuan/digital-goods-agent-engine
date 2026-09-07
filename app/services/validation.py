from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict, stable_hash
from .opportunities import get_opportunity


POLICY_VERSION = "commercial-validation-v1-real-demand"


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _validation_rows(opportunity_id: int) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            """SELECT e.id,e.platform,e.author,e.published_at,e.published_time_quality,
                      e.evidence_quality_score,e.content_fingerprint,e.canonical_key,e.raw_hash,
                      e.intent_class,e.intent_confidence,e.intent_weight,e.content_kind
               FROM opportunity_evidence oe
               JOIN evidence e ON e.id=oe.evidence_id
               WHERE oe.opportunity_id=? AND oe.relation_type='demand'
                 AND e.intent_class IN ('explicit_demand','probable_demand')
               ORDER BY e.id""",
            (opportunity_id,),
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def current_snapshot(opportunity_id: int) -> tuple[str, list[dict[str, Any]]]:
    opportunity = get_opportunity(opportunity_id)
    if not opportunity:
        raise KeyError("opportunity not found")
    rows = _validation_rows(opportunity_id)
    evidence_state = [
        (
            row.get("id"),
            row.get("raw_hash"),
            row.get("intent_class"),
            row.get("intent_confidence"),
            row.get("intent_weight"),
        )
        for row in rows
    ]
    snapshot_hash = stable_hash(
        POLICY_VERSION,
        opportunity.get("cluster_signature"),
        dumps(evidence_state),
    )
    return snapshot_hash, rows


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprints = {
        str(row.get("content_fingerprint") or row.get("canonical_key") or row.get("id"))
        for row in rows
    }
    authors = {
        f"{row.get('platform')}:{str(row.get('author')).strip().lower()}"
        for row in rows
        if row.get("author") and str(row.get("author")).strip()
    }
    platforms = {str(row.get("platform")) for row in rows if row.get("platform")}
    explicit_count = sum(row.get("intent_class") == "explicit_demand" for row in rows)
    probable_count = sum(row.get("intent_class") == "probable_demand" for row in rows)
    comments = sum(row.get("content_kind") == "comment" for row in rows)
    quality = [float(row.get("evidence_quality_score") or 0) for row in rows]
    timestamp_points = 0.0
    recent_30d = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for row in rows:
        time_quality = row.get("published_time_quality")
        if time_quality == "source":
            timestamp_points += 1.0
        elif time_quality == "inherited_thread":
            timestamp_points += 0.4
        published = _parse_datetime(row.get("published_at"))
        if published and cutoff <= published <= datetime.now(timezone.utc):
            recent_30d += 1
    return {
        "real_demand_count": len(rows),
        "effective_signal_count": len(fingerprints),
        "unique_authors": len(authors),
        "platform_count": len(platforms),
        "explicit_demand_count": int(explicit_count),
        "probable_demand_count": int(probable_count),
        "comment_demand_count": int(comments),
        "recent_30d_count": recent_30d,
        "average_evidence_quality": round(sum(quality) / len(quality), 2) if quality else 0.0,
        "timestamp_coverage_pct": round(timestamp_points / len(rows) * 100.0, 2) if rows else 0.0,
    }


def _score(metrics: dict[str, Any]) -> float:
    min_signals = max(1, settings.commercial_validation_min_signals)
    min_authors = max(1, settings.commercial_validation_min_authors)
    signal = min(1.0, metrics["effective_signal_count"] / min_signals) * 30.0
    explicit = min(1.0, metrics["explicit_demand_count"]) * 15.0
    diversity_ratio = max(
        min(1.0, metrics["unique_authors"] / min_authors),
        min(1.0, metrics["platform_count"] / 2.0),
    )
    diversity = diversity_ratio * 20.0
    quality = min(1.0, metrics["average_evidence_quality"] / 100.0) * 15.0
    timestamps = min(1.0, metrics["timestamp_coverage_pct"] / 100.0) * 10.0
    recency = 10.0 if metrics["recent_30d_count"] > 0 else 0.0
    return round(signal + explicit + diversity + quality + timestamps + recency, 2)


def validate_opportunity(opportunity_id: int) -> dict[str, Any]:
    opportunity = get_opportunity(opportunity_id)
    if not opportunity:
        raise KeyError("opportunity not found")
    if not int(opportunity.get("cluster_active") or 0):
        raise ValueError("inactive opportunity cannot be validated")
    snapshot_hash, rows = current_snapshot(opportunity_id)
    metrics = _metrics(rows)
    score = _score(metrics)
    reason_codes: list[str] = []
    if metrics["effective_signal_count"] < settings.commercial_validation_min_signals:
        reason_codes.append("insufficient_independent_demand")
    if metrics["explicit_demand_count"] < 1:
        reason_codes.append("no_explicit_demand")
    if (
        metrics["unique_authors"] < settings.commercial_validation_min_authors
        and metrics["platform_count"] < 2
    ):
        reason_codes.append("insufficient_author_or_platform_diversity")
    if metrics["average_evidence_quality"] < settings.commercial_validation_min_quality:
        reason_codes.append("low_evidence_quality")
    if metrics["timestamp_coverage_pct"] < settings.commercial_validation_min_timestamp_coverage:
        reason_codes.append("low_timestamp_coverage")
    if metrics["recent_30d_count"] < 1:
        reason_codes.append("no_recent_demand")
    if score < settings.commercial_validation_min_score:
        reason_codes.append("validation_score_below_threshold")
    status = "PASSED" if not reason_codes else "FAILED"
    created_at = now_iso()
    result = {
        "opportunity_id": opportunity_id,
        "status": status,
        "score": score,
        "policy_version": POLICY_VERSION,
        "snapshot_hash": snapshot_hash,
        "metrics": metrics,
        "reason_codes": reason_codes,
        "thresholds": {
            "min_signals": settings.commercial_validation_min_signals,
            "min_authors": settings.commercial_validation_min_authors,
            "min_quality": settings.commercial_validation_min_quality,
            "min_timestamp_coverage": settings.commercial_validation_min_timestamp_coverage,
            "min_score": settings.commercial_validation_min_score,
        },
        "created_at": created_at,
        "data_policy": "deterministic gate over stored real demand evidence; no AI call and no synthetic metrics",
    }
    with conn() as c:
        c.execute(
            """INSERT INTO commercial_validation_runs(
                   opportunity_id,status,score,policy_version,snapshot_hash,metrics_json,reasons_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                opportunity_id,
                status,
                score,
                POLICY_VERSION,
                snapshot_hash,
                dumps(metrics),
                dumps(reason_codes),
                created_at,
            ),
        )
        c.execute(
            """UPDATE opportunities SET validation_status=?,validation_score=?,validation_snapshot_hash=?,
                      validated_at=?,validation_json=?,updated_at=? WHERE id=?""",
            (status, score, snapshot_hash, created_at, dumps(result), created_at, opportunity_id),
        )
    return result


def get_validation(opportunity_id: int) -> dict[str, Any]:
    opportunity = get_opportunity(opportunity_id)
    if not opportunity:
        raise KeyError("opportunity not found")
    snapshot_hash, _ = current_snapshot(opportunity_id)
    with conn() as c:
        row = c.execute(
            "SELECT * FROM commercial_validation_runs WHERE opportunity_id=? ORDER BY id DESC LIMIT 1",
            (opportunity_id,),
        ).fetchone()
    if not row:
        return {
            "opportunity_id": opportunity_id,
            "status": "NOT_VALIDATED",
            "is_current": False,
            "current_snapshot_hash": snapshot_hash,
        }
    result = row_to_dict(row) or {}
    result["metrics"] = loads(result.pop("metrics_json", None), {})
    result["reason_codes"] = loads(result.pop("reasons_json", None), [])
    result["is_current"] = result.get("snapshot_hash") == snapshot_hash
    result["current_snapshot_hash"] = snapshot_hash
    if not result["is_current"]:
        result["status"] = "STALE"
    return result


def require_current_validation(opportunity_id: int) -> dict[str, Any]:
    result = get_validation(opportunity_id)
    if result.get("status") != "PASSED" or not result.get("is_current"):
        raise ValueError("current commercial validation must pass before paid AI analysis or product generation")
    return result


def validate_active_opportunities(limit: int = 100) -> dict[str, Any]:
    with conn() as c:
        rows = c.execute(
            """SELECT id FROM opportunities WHERE cluster_active=1
               ORDER BY coalesce(trusted_trend_score,0) DESC,id DESC LIMIT ?""",
            (max(1, min(limit, 500)),),
        ).fetchall()
    results = []
    for row in rows:
        opportunity_id = int(row["id"])
        try:
            results.append(validate_opportunity(opportunity_id))
        except Exception as exc:
            results.append({"opportunity_id": opportunity_id, "status": "ERROR", "error": str(exc)})
    return {
        "processed": len(results),
        "passed": sum(item.get("status") == "PASSED" for item in results),
        "failed": sum(item.get("status") == "FAILED" for item in results),
        "results": results,
    }
