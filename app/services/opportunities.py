from __future__ import annotations

from typing import Any

from ..db import conn, loads, row_to_dict
from .clustering import rebuild_clusters
from .market_intel import rematch_market_intel


def build_from_unlinked_evidence(limit: int = 500) -> dict[str, Any]:
    # `limit` is retained for API compatibility. Clustering uses the configured rolling window
    # and evidence cap so old + new real evidence are regrouped consistently.
    result = rebuild_clusters()
    result["market_intel_links"] = rematch_market_intel()
    return result


def get_opportunity(opportunity_id: int) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute("SELECT * FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    return row_to_dict(row)


def analysis_is_current(opportunity: dict[str, Any]) -> bool:
    if (opportunity.get("overall_score") is None or bool(opportunity.get("analysis_stale"))
            or not bool(opportunity.get("cluster_active"))):
        return False
    analysis = loads(opportunity.get("analysis_json"), {}) or {}
    snapshot = analysis.get("snapshot_hash")
    if snapshot:
        # Market observations are part of the Agent snapshot even when demand Gate A
        # remains unchanged. Check at every consequential boundary, not just in the UI.
        from .opportunity_agent import build_evidence_digest
        return snapshot == build_evidence_digest(int(opportunity["id"]))["snapshot_hash"]
    return True  # Legacy V1.2 analysis uses the persisted analysis_stale flag.


def get_evidence_for_opportunity(opportunity_id: int, limit: int = 80) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            """
            SELECT e.*, oe.relation_type
            FROM opportunity_evidence oe
            JOIN evidence e ON e.id=oe.evidence_id
            WHERE oe.opportunity_id=?
            ORDER BY CASE oe.relation_type WHEN 'demand' THEN 0 ELSE 1 END,
                     coalesce(e.published_at,e.collected_at) DESC LIMIT ?
            """,
            (opportunity_id, max(1, min(limit, 300))),
        ).fetchall()
        link_rows = c.execute(
            """SELECT es.evidence_id,s.id source_id,s.name source_name,s.query source_query,s.platform source_platform,s.config_json
               FROM evidence_sources es
               JOIN sources s ON s.id=es.source_id
               JOIN opportunity_evidence oe ON oe.evidence_id=es.evidence_id
               WHERE oe.opportunity_id=?""",
            (opportunity_id,),
        ).fetchall()
    links: dict[int, list[dict[str, Any]]] = {}
    for r in link_rows:
        d = dict(r)
        cfg = loads(d.pop("config_json", None), {}) or {}
        d["role"] = cfg.get("role", "demand")
        links.setdefault(int(d["evidence_id"]), []).append(d)
    result = []
    for row in rows:
        d = row_to_dict(row) or {}
        d["engagement"] = loads(d.pop("engagement_json", None), {})
        d["raw"] = loads(d.pop("raw_json", None), {})
        d["source_matches"] = links.get(int(d["id"]), [])
        result.append(d)
    return result


def list_opportunities(limit: int = 100, status: str | None = None, cross_source_only: bool = False, include_inactive: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM opportunities"
    where = []
    params: list[Any] = []
    if not include_inactive:
        where.append("cluster_active=1")
    if status:
        where.append("status=?")
        params.append(status)
    if cross_source_only:
        where.append("platform_count>=2")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY coalesce(trusted_trend_score,-1) DESC,coalesce(trend_score,-1) DESC,coalesce(overall_score,-1) DESC,id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
    return [row_to_dict(r) or {} for r in rows]
