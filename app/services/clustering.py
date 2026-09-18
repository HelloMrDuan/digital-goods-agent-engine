from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings
from ..db import conn, loads, now_iso, row_to_dict, stable_hash
from . import clustering_legacy as _legacy

_METHOD = "real_evidence_tfidf_v6_evidence_first"
_CONTEXT_MIN_QUALITY = 45.0


def _load_context_rows(strict_ids: set[int]) -> list[dict[str, Any]]:
    """Load real core-demand discussions as AI context candidates.

    These rows may form candidate clusters, but they are persisted as relation_type=context,
    therefore they never inflate deterministic demand counts, trend scores or commercial
    validation. Supply/showcase/noise remain excluded.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(settings.cluster_window_days, settings.trend_window_days + 14))
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM evidence WHERE intent_class='discussion' ORDER BY id DESC LIMIT ?",
            (max(50, min(settings.cluster_max_evidence, 5000)),),
        ).fetchall()
        source_rows = c.execute("SELECT id,name,platform,query,config_json FROM sources").fetchall()
        link_rows = c.execute("SELECT evidence_id,source_id FROM evidence_sources").fetchall()

    sources = {int(r["id"]): {**dict(r), "config": loads(r["config_json"], {}) or {}} for r in source_rows}
    links: dict[int, list[int]] = defaultdict(list)
    for row in link_rows:
        links[int(row["evidence_id"])].append(int(row["source_id"]))

    result: list[dict[str, Any]] = []
    for row in rows:
        data = row_to_dict(row) or {}
        evidence_id = int(data["id"])
        if evidence_id in strict_ids:
            continue
        if float(data.get("evidence_quality_score") or 0) < _CONTEXT_MIN_QUALITY:
            continue
        event_dt = _legacy._event_dt(data)
        if event_dt < cutoff:
            continue
        linked = [sources[sid] for sid in links.get(evidence_id, []) if sid in sources]
        if not linked:
            source = sources.get(int(data["source_id"]))
            linked = [source] if source else []
        eligible = False
        for source in linked:
            cfg = source.get("config") or {}
            role = str(cfg.get("role") or "demand").lower()
            platform = str(source.get("platform") or "").lower()
            group = cfg.get("business_group")
            if not group:
                group = "auxiliary" if platform == "v2ex" else {"demand": "core_demand", "trend": "trend_support", "supply": "supply_intel"}.get(role, "auxiliary")
            if role == "demand" and str(group) == "core_demand":
                eligible = True
                break
        if not eligible:
            continue
        data["_event_dt"] = event_dt
        data["_sources"] = linked
        data["_relation_type"] = "context"
        result.append(data)
    return result


def _load_candidates() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, str], int, int]:
    strict_demand, support = _legacy._load_evidence()
    relation_by_id: dict[int, str] = {}
    for row in strict_demand:
        row["_relation_type"] = "demand"
        relation_by_id[int(row["id"])] = "demand"
    context = _load_context_rows(set(relation_by_id))
    for row in context:
        relation_by_id[int(row["id"])] = "context"
    return strict_demand + context, support, relation_by_id, len(strict_demand), len(context)


def _existing_for_cluster(c, evidence_ids: list[int], candidate_key: str) -> int | None:
    if evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = c.execute(
            f"""SELECT oe.opportunity_id,COUNT(*) overlap_count,
                       (SELECT COUNT(*) FROM opportunity_evidence x
                        WHERE x.opportunity_id=oe.opportunity_id AND x.relation_type IN ('demand','context')) old_count
                FROM opportunity_evidence oe
                JOIN opportunities o ON o.id=oe.opportunity_id
                WHERE oe.evidence_id IN ({placeholders})
                  AND oe.relation_type IN ('demand','context')
                  AND o.cluster_method IN (?,?,?,?,?,?)
                GROUP BY oe.opportunity_id ORDER BY overlap_count DESC""",
            tuple(evidence_ids) + (
                _METHOD,
                "real_evidence_tfidf_v5_content_intent_gate",
                "real_evidence_tfidf_v4_business_priority",
                "real_evidence_tfidf_v3",
                "real_evidence_tfidf_v2",
                "real_evidence_tfidf_v1",
            ),
        ).fetchall()
        best: tuple[float, int] | None = None
        new_count = len(evidence_ids)
        for row in rows:
            overlap = int(row["overlap_count"])
            old_count = max(1, int(row["old_count"] or 0))
            union = new_count + old_count - overlap
            jaccard = overlap / max(1, union)
            containment = overlap / max(1, min(new_count, old_count))
            score = max(jaccard, containment * 0.82)
            if overlap >= 1 and (best is None or score > best[0]):
                best = (score, int(row["opportunity_id"]))
        if best and best[0] >= 0.32:
            return best[1]
    row = c.execute("SELECT id FROM opportunities WHERE candidate_key=?", (candidate_key,)).fetchone()
    return int(row["id"]) if row else None


def rebuild_clusters() -> dict[str, Any]:
    started = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO cluster_runs(status,method,window_days,threshold,started_at) VALUES(?,?,?,?,?)",
            ("RUNNING", _METHOD, settings.cluster_window_days, settings.cluster_similarity_threshold, started),
        )
        run_id = int(cur.lastrowid)

    try:
        candidate_rows, support_rows, relation_by_id, demand_count, context_count = _load_candidates()
        clusters, vectors, idf = _legacy._cluster_rows(candidate_rows)
        active_ids: list[int] = []
        cross_platform = 0
        support_links = 0

        # The active-cluster switch and all opportunity/link mutations are one SQLite
        # transaction. If any cluster fails, rollback restores the previously active set.
        with conn() as c:
            c.execute(
                "UPDATE opportunities SET cluster_active=0 WHERE cluster_method IN (?,?,?,?,?,?)",
                (
                    _METHOD,
                    "real_evidence_tfidf_v5_content_intent_gate",
                    "real_evidence_tfidf_v4_business_priority",
                    "real_evidence_tfidf_v3",
                    "real_evidence_tfidf_v2",
                    "real_evidence_tfidf_v1",
                ),
            )
            for cluster in clusters:
                rep = _legacy._representative(cluster, vectors)
                signature = _legacy._signature(cluster)
                candidate_key = stable_hash("cluster-v6-evidence-first", signature)
                evidence_ids = sorted({int(row["id"]) for row in cluster})
                support_ids = _legacy._support_matches(cluster, support_rows, vectors, idf)
                platforms = {row.get("platform") for row in cluster if row.get("platform")}
                if len(platforms) >= 2:
                    cross_platform += 1

                opportunity_id = _existing_for_cluster(c, evidence_ids, candidate_key)
                now = now_iso()
                if opportunity_id is None:
                    cur = c.execute(
                        """INSERT INTO opportunities(title,candidate_key,normalized_need,status,evidence_count,cluster_method,cluster_signature,
                           platform_count,cluster_active,last_cluster_run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            rep.get("title") or signature,
                            candidate_key,
                            rep.get("title") or signature,
                            "NEW",
                            0,
                            _METHOD,
                            signature,
                            len(platforms),
                            1,
                            run_id,
                            now,
                            now,
                        ),
                    )
                    opportunity_id = int(cur.lastrowid)
                else:
                    old_ids = {
                        int(row["evidence_id"])
                        for row in c.execute(
                            "SELECT evidence_id FROM opportunity_evidence WHERE opportunity_id=? AND relation_type IN ('demand','context')",
                            (opportunity_id,),
                        ).fetchall()
                    }
                    existing = c.execute(
                        "SELECT analysis_json,candidate_key FROM opportunities WHERE id=?",
                        (opportunity_id,),
                    ).fetchone()
                    changed = old_ids != set(evidence_ids)
                    stale = 1 if changed and existing and existing["analysis_json"] else 0
                    if changed:
                        c.execute("UPDATE opportunities SET validation_status='STALE' WHERE id=?", (opportunity_id,))
                    owner = c.execute("SELECT id FROM opportunities WHERE candidate_key=?", (candidate_key,)).fetchone()
                    safe_key = candidate_key if not owner or int(owner["id"]) == opportunity_id else existing["candidate_key"]
                    title = rep.get("title") if not (existing and existing["analysis_json"]) else None
                    if title:
                        c.execute(
                            """UPDATE opportunities SET title=?,candidate_key=?,cluster_method=?,cluster_signature=?,platform_count=?,analysis_stale=?,
                               cluster_active=1,last_cluster_run_id=?,updated_at=? WHERE id=?""",
                            (title, safe_key, _METHOD, signature, len(platforms), stale, run_id, now, opportunity_id),
                        )
                    else:
                        c.execute(
                            """UPDATE opportunities SET candidate_key=?,cluster_method=?,cluster_signature=?,platform_count=?,analysis_stale=?,
                               cluster_active=1,last_cluster_run_id=?,updated_at=? WHERE id=?""",
                            (safe_key, _METHOD, signature, len(platforms), stale, run_id, now, opportunity_id),
                        )
                    c.execute("DELETE FROM opportunity_evidence WHERE opportunity_id=?", (opportunity_id,))

                for evidence_id in evidence_ids:
                    relation = relation_by_id.get(evidence_id, "context")
                    c.execute(
                        "INSERT OR IGNORE INTO opportunity_evidence(opportunity_id,evidence_id,relation_type) VALUES(?,?,?)",
                        (opportunity_id, evidence_id, relation),
                    )
                for evidence_id in support_ids:
                    c.execute(
                        "INSERT OR IGNORE INTO opportunity_evidence(opportunity_id,evidence_id,relation_type) VALUES(?,?, 'support')",
                        (opportunity_id, evidence_id),
                    )
                    support_links += 1
                active_ids.append(opportunity_id)

        trend_errors: list[dict[str, Any]] = []
        for opportunity_id in active_ids:
            try:
                _legacy.calculate_trend(opportunity_id)
            except Exception as exc:
                trend_errors.append({"opportunity_id": opportunity_id, "error": str(exc)})

        finished = now_iso()
        run_status = "SUCCESS" if not trend_errors else "PARTIAL"
        with conn() as c:
            c.execute(
                """UPDATE cluster_runs SET status=?,finished_at=?,evidence_count=?,cluster_count=?,cross_platform_cluster_count=?,error=? WHERE id=?""",
                (
                    run_status,
                    finished,
                    len(candidate_rows),
                    len(clusters),
                    cross_platform,
                    None if not trend_errors else dumps(trend_errors[:10]),
                    run_id,
                ),
            )
        return {
            "run_id": run_id,
            "status": run_status,
            "method": _METHOD,
            "processed_evidence": demand_count,
            "context_evidence_considered": context_count,
            "candidate_evidence_total": len(candidate_rows),
            "support_evidence_considered": len(support_rows),
            "support_links": support_links,
            "candidate_groups": len(clusters),
            "cross_platform_groups": cross_platform,
            "opportunity_ids": active_ids,
            "trend_errors": trend_errors,
            "threshold": settings.cluster_similarity_threshold,
            "support_threshold": settings.trend_support_similarity_threshold,
            "window_days": settings.cluster_window_days,
            "policy": "explicit/probable demand counts as demand; high-quality core-demand discussion may form AI context candidates but is stored as context and never inflates deterministic demand/trend/validation; supply/showcase/noise remain excluded",
        }
    except Exception as exc:
        with conn() as c:
            c.execute(
                "UPDATE cluster_runs SET status='FAILED',finished_at=?,error=? WHERE id=?",
                (now_iso(), str(exc), run_id),
            )
        raise

def calculate_trend(opportunity_id: int, calculated_at: datetime | None = None) -> dict[str, Any]:
    return _legacy.calculate_trend(opportunity_id, calculated_at)


def trend_detail(opportunity_id: int) -> dict[str, Any] | None:
    return _legacy.trend_detail(opportunity_id)


def trend_ranking(limit: int = 50, cross_source_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM opportunities WHERE cluster_method=? AND cluster_active=1"
    params: list[Any] = [_METHOD]
    if cross_source_only:
        sql += " AND platform_count>=2"
    sql += " ORDER BY coalesce(trusted_trend_score,0) DESC,coalesce(trend_score,0) DESC,current_7d DESC,evidence_count DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def latest_cluster_run() -> dict[str, Any] | None:
    return _legacy.latest_cluster_run()
