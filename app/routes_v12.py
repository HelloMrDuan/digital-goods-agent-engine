from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .config import settings
from .db import conn, dumps, loads, now_iso, row_to_dict
from .services.analyze import analyze_one
from .services.demand_discovery import run_demand_discovery
from .services.opportunities import build_from_unlinked_evidence, get_evidence_for_opportunity, get_opportunity
from .services.validation import evaluate_gate_a, get_gate_a
from .services.opportunity_agent import (
    get_card_for_opportunity,
    get_opportunity_card,
    list_agent_runs,
    list_opportunity_cards,
)

router = APIRouter()
_MAX_REBUILD_CONCURRENCY = 6
_RECENT_DEMAND_DAYS = 30


def _rebuild_worker_count(total: int) -> int:
    if total <= 0:
        return 0
    configured = max(1, int(getattr(settings, "agent_rebuild_concurrency", 3) or 1))
    return min(configured, _MAX_REBUILD_CONCURRENCY, total)


def _ensure_rebuild_schema() -> None:
    with conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_rebuild_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT,
                message TEXT,
                current_opportunity_id INTEGER,
                analyze_enabled INTEGER NOT NULL DEFAULT 1,
                refresh_enabled INTEGER NOT NULL DEFAULT 1,
                requested_limit INTEGER NOT NULL DEFAULT 8,
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                succeeded INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {str(row["name"]) for row in c.execute("PRAGMA table_info(opportunity_rebuild_tasks)").fetchall()}
        if "refresh_enabled" not in columns:
            c.execute("ALTER TABLE opportunity_rebuild_tasks ADD COLUMN refresh_enabled INTEGER NOT NULL DEFAULT 1")


def _update_rebuild(task_id: int, **values: Any) -> None:
    if not values:
        return
    values["updated_at"] = now_iso()
    fields = ",".join(f"{key}=?" for key in values)
    with conn() as c:
        c.execute(
            f"UPDATE opportunity_rebuild_tasks SET {fields} WHERE id=?",
            (*values.values(), task_id),
        )


def _analysis_task_start(opportunity_id: int) -> int:
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO analysis_tasks(opportunity_id,status,progress,stage,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (opportunity_id, "PENDING", 0, "排队", "等待多 Agent 商机分析", now, now),
        )
    return int(cur.lastrowid)


def _analysis_task_finish(task_id: int, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    with conn() as c:
        if error:
            c.execute(
                "UPDATE analysis_tasks SET status='FAILED',progress=100,stage='失败',message=?,error=?,updated_at=? WHERE id=?",
                (error, error, now_iso(), task_id),
            )
        else:
            c.execute(
                "UPDATE analysis_tasks SET status='SUCCESS',progress=100,stage='完成',message='Opportunity Card 已生成',result_json=?,updated_at=? WHERE id=?",
                (dumps(result or {}), now_iso(), task_id),
            )


def _run_rebuild_opportunity(opportunity_id: int, analysis_task_id: int) -> dict[str, Any]:
    try:
        result = analyze_one(opportunity_id)
        _analysis_task_finish(analysis_task_id, result=result)
        return {"opportunity_id": opportunity_id, "status": "SUCCESS", "result": result}
    except Exception as exc:
        message = str(exc)
        _analysis_task_finish(analysis_task_id, error=message)
        return {"opportunity_id": opportunity_id, "status": "FAILED", "error": message}


def _run_rebuild(task_id: int, analyze_enabled: bool, limit: int, refresh_enabled: bool, refresh_queries_per_probe: int) -> None:
    results: list[dict[str, Any]] = []
    discovery_result: dict[str, Any] | None = None
    try:
        if refresh_enabled:
            _update_rebuild(
                task_id,
                status="RUNNING",
                progress=3,
                stage="刷新真实需求",
                message="正在用高意图关键词矩阵刷新小红书/Reddit最近30天真实需求",
            )
            def refresh_progress(event: dict[str, Any]) -> None:
                completed = int(event.get("completed_queries") or 0)
                total_queries = max(1, int(event.get("total_queries") or 1))
                progress = min(9, 3 + int(completed / total_queries * 6))
                platform = str(event.get("platform") or "")
                query = str(event.get("query") or "")
                fetched = int(event.get("fetched") or 0)
                error = event.get("error")
                status_text = f"失败: {str(error)[:100]}" if error else f"返回 {fetched} 条"
                _update_rebuild(
                    task_id,
                    progress=progress,
                    stage=f"刷新真实需求 {completed}/{total_queries}",
                    message=f"{platform} | {query} | {status_text}",
                )

            discovery_result = run_demand_discovery(
                max_queries_per_probe=refresh_queries_per_probe,
                progress_callback=refresh_progress,
            )
            _update_rebuild(
                task_id,
                progress=10,
                stage="需求刷新完成",
                message=(
                    f"保留近期证据 {int(discovery_result.get('retained_recent') or 0)} 条；"
                    f"直接需求信号 {int(discovery_result.get('direct_demand_signals') or 0)} 条"
                ),
            )

        _update_rebuild(task_id, status="RUNNING", progress=12, stage="重建候选", message="正在从最新真实 evidence 重建商机候选")
        cluster_result = build_from_unlinked_evidence()
        if not analyze_enabled:
            payload = {
                "demand_discovery": discovery_result,
                "cluster_run": cluster_result,
                "agent_analysis": [],
            }
            _update_rebuild(
                task_id,
                status="SUCCESS",
                progress=100,
                stage="完成",
                message="真实需求刷新和候选重建完成；未执行付费 AI",
                result_json=dumps(payload),
            )
            return

        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=_RECENT_DEMAND_DAYS)).isoformat(timespec="seconds")
        with conn() as c:
            rows = c.execute(
                """SELECT id FROM opportunities
                   WHERE cluster_active=1
                     AND coalesce(last_seen_at,'')>=?
                     AND (coalesce(explicit_demand_count,0)+coalesce(probable_demand_count,0))>0
                   ORDER BY CASE WHEN validation_status='PASSED' THEN 1 ELSE 0 END DESC,
                            coalesce(effective_signal_count,0) DESC,
                            coalesce(platform_count,0) DESC,
                            coalesce(trusted_trend_score,0) DESC,
                            coalesce(data_quality_score,0) DESC,
                            evidence_count DESC,id DESC
                   LIMIT ?""",
                (recent_cutoff, limit),
            ).fetchall()
        ids = [int(row["id"]) for row in rows]
        total = len(ids)
        worker_count = _rebuild_worker_count(total)
        _update_rebuild(
            task_id,
            progress=15,
            stage="准备 Agent",
            message=f"最近 {_RECENT_DEMAND_DAYS} 天有效需求候选 {total} 个；并发 {worker_count} 路",
            total=total,
            current_opportunity_id=ids[0] if ids else None,
        )

        succeeded = 0
        failed = 0
        if total:
            analysis_task_ids = {opportunity_id: _analysis_task_start(opportunity_id) for opportunity_id in ids}
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="opportunity-agent") as pool:
                future_map = {
                    pool.submit(_run_rebuild_opportunity, opportunity_id, analysis_task_ids[opportunity_id]): opportunity_id
                    for opportunity_id in ids
                }
                processed = 0
                for future in as_completed(future_map):
                    opportunity_id = future_map[future]
                    try:
                        item = future.result()
                    except Exception as exc:
                        message = str(exc)
                        _analysis_task_finish(analysis_task_ids[opportunity_id], error=message)
                        item = {"opportunity_id": opportunity_id, "status": "FAILED", "error": message}
                    results.append(item)
                    processed += 1
                    if item.get("status") == "SUCCESS":
                        succeeded += 1
                    else:
                        failed += 1

                    active_ids = [
                        oid
                        for pending_future, oid in future_map.items()
                        if not pending_future.done()
                    ]
                    progress = 15 + int(processed / max(1, total) * 80)
                    _update_rebuild(
                        task_id,
                        progress=progress,
                        stage=f"多 Agent 并发 {processed}/{total}",
                        message=f"已完成 {processed}/{total}；成功 {succeeded}，失败 {failed}；并发 {worker_count} 路",
                        current_opportunity_id=active_ids[0] if active_ids else None,
                        processed=processed,
                        succeeded=succeeded,
                        failed=failed,
                    )

        if total == 0 or failed == 0:
            status = "SUCCESS"
        elif succeeded > 0:
            status = "PARTIAL"
        else:
            status = "FAILED"
        result_by_id = {int(item["opportunity_id"]): item for item in results}
        ordered_results = [result_by_id[opportunity_id] for opportunity_id in ids if opportunity_id in result_by_id]
        payload = {
            "demand_discovery": discovery_result,
            "cluster_run": cluster_result,
            "agent_analysis": ordered_results,
            "concurrency": worker_count,
            "selection_policy": {
                "recent_demand_days": _RECENT_DEMAND_DAYS,
                "requires_explicit_or_probable_demand": True,
                "order": "validation, signal_count, platform_diversity, trusted_trend, data_quality",
            },
        }
        final_message = (
            f"最近 {_RECENT_DEMAND_DAYS} 天没有可进入 Agent 的真实需求候选"
            if total == 0
            else f"处理 {total} 个商机：成功 {succeeded}，失败 {failed}；并发 {worker_count} 路"
        )
        _update_rebuild(
            task_id,
            status=status,
            progress=100,
            stage="完成" if status != "FAILED" else "失败",
            message=final_message,
            current_opportunity_id=None,
            processed=total,
            succeeded=succeeded,
            failed=failed,
            result_json=dumps(payload),
            error=None if status != "FAILED" else "all opportunity agent analyses failed",
        )
    except Exception as exc:
        _update_rebuild(
            task_id,
            status="FAILED",
            progress=100,
            stage="失败",
            message=str(exc),
            error=str(exc),
            result_json=dumps({"demand_discovery": discovery_result, "agent_analysis": results}),
        )


def _rebuild_task_payload(task_id: int) -> dict[str, Any]:
    _ensure_rebuild_schema()
    with conn() as c:
        row = c.execute("SELECT * FROM opportunity_rebuild_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise KeyError("rebuild task not found")
    data = row_to_dict(row) or {}
    data["analyze_enabled"] = bool(data.get("analyze_enabled"))
    data["refresh_enabled"] = bool(data.get("refresh_enabled"))
    data["result"] = loads(data.pop("result_json", None), None)
    data["concurrency"] = _rebuild_worker_count(int(data.get("total") or data.get("requested_limit") or 0)) if data["analyze_enabled"] else 0
    current_id = data.get("current_opportunity_id")
    data["current_analysis"] = None
    if current_id:
        with conn() as c:
            analysis = c.execute(
                "SELECT id,status,progress,stage,message,error,updated_at FROM analysis_tasks WHERE opportunity_id=? ORDER BY id DESC LIMIT 1",
                (int(current_id),),
            ).fetchone()
        if analysis:
            data["current_analysis"] = row_to_dict(analysis)
    return data


@router.post("/api/opportunities/rebuild")
def opportunities_rebuild(
    analyze: bool = True,
    refresh: bool = True,
    limit: int = Query(default=8, ge=1, le=20),
    refresh_queries_per_probe: int = Query(default=3, ge=1, le=8),
):
    _ensure_rebuild_schema()
    if analyze and not settings.is_llm_configured:
        raise HTTPException(409, "LLM is not configured; set Qiniu or SenseNova, or call with analyze=false")
    now = now_iso()
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    with conn() as c:
        # Serialize the active-task check and insert so two rapid clicks/processes cannot
        # both pass the check and start duplicate paid Agent work.
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """UPDATE opportunity_rebuild_tasks
               SET status='FAILED',progress=100,stage='失败',message='stale rebuild task expired',
                   error='stale rebuild task expired',updated_at=?
               WHERE status IN ('PENDING','RUNNING') AND updated_at<?""",
            (now, stale_cutoff),
        )
        active = c.execute(
            """SELECT id,status,progress,stage,created_at,updated_at
               FROM opportunity_rebuild_tasks
               WHERE status IN ('PENDING','RUNNING')
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if active:
            raise HTTPException(
                409,
                detail={
                    "message": "opportunity rebuild already running",
                    "task_id": int(active["id"]),
                    "status": active["status"],
                    "progress": int(active["progress"] or 0),
                    "stage": active["stage"],
                },
            )
        cur = c.execute(
            """INSERT INTO opportunity_rebuild_tasks(
                   status,progress,stage,message,analyze_enabled,refresh_enabled,requested_limit,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("PENDING", 0, "排队", "等待刷新真实需求并重建", 1 if analyze else 0, 1 if refresh else 0, limit, now, now),
        )
        task_id = int(cur.lastrowid)
    threading.Thread(target=_run_rebuild, args=(task_id, analyze, limit, refresh, refresh_queries_per_probe), daemon=True).start()
    return {
        "task_id": task_id,
        "status": "PENDING",
        "analyze": analyze,
        "refresh": refresh,
        "limit": limit,
        "refresh_queries_per_probe": refresh_queries_per_probe,
        "concurrency": _rebuild_worker_count(limit) if analyze else 0,
    }


@router.get("/api/opportunities/rebuild/tasks/{task_id}")
def opportunity_rebuild_task(task_id: int):
    try:
        return _rebuild_task_payload(task_id)
    except KeyError:
        raise HTTPException(404, "rebuild task not found")


@router.get("/api/opportunity-cards")
def opportunity_cards_list(
    decision: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    if decision and decision.upper() not in {"BUILD", "WATCH", "DROP"}:
        raise HTTPException(400, "decision must be BUILD, WATCH or DROP")
    cards = list_opportunity_cards(limit=limit, decision=decision)
    if not cards:
        return []
    ids = [int(card["opportunity_id"]) for card in cards]
    placeholders = ",".join("?" for _ in ids)
    with conn() as c:
        rows = c.execute(
            f"SELECT id,analysis_stale,validation_status,validation_score,trend_score,trusted_trend_score,data_quality_score FROM opportunities WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    metadata = {int(row["id"]): row_to_dict(row) or {} for row in rows}
    for card in cards:
        meta = metadata.get(int(card["opportunity_id"]), {})
        card["stale"] = bool(meta.get("analysis_stale"))
        card["validation_status"] = meta.get("validation_status")
        card["validation_score"] = meta.get("validation_score")
        card["trend_score"] = meta.get("trend_score")
        card["trusted_trend_score"] = meta.get("trusted_trend_score")
        card["data_quality_score"] = meta.get("data_quality_score")
        try:
            card["gate_a"] = get_gate_a(int(card["opportunity_id"]))
        except Exception as exc:
            card["gate_a"] = {"decision": "REVIEW_REQUIRED", "error": str(exc)}
    return cards


@router.get("/api/opportunity-cards/by-opportunity/{opportunity_id}")
def opportunity_card_by_opportunity(opportunity_id: int):
    card = get_card_for_opportunity(opportunity_id)
    if not card:
        raise HTTPException(404, "opportunity card not found")
    card["agent_runs"] = list_agent_runs(opportunity_id, 30)
    return card


@router.get("/api/opportunity-cards/{card_id}")
def opportunity_card_detail(card_id: int):
    card = get_opportunity_card(card_id)
    if not card:
        raise HTTPException(404, "opportunity card not found")
    opportunity_id = int(card["opportunity_id"])
    card["opportunity"] = get_opportunity(opportunity_id)
    card["evidence"] = get_evidence_for_opportunity(opportunity_id, 40)
    card["agent_runs"] = list_agent_runs(opportunity_id, 30)
    return card


@router.get("/api/opportunities/{opportunity_id}/agent-runs")
def opportunity_agent_runs(opportunity_id: int, limit: int = Query(default=50, ge=1, le=200)):
    if not get_opportunity(opportunity_id):
        raise HTTPException(404, "opportunity not found")
    return list_agent_runs(opportunity_id, limit)


@router.get("/api/opportunities/{opportunity_id}/gate-a")
def opportunity_gate_a_get(opportunity_id: int):
    try:
        return get_gate_a(opportunity_id)
    except KeyError:
        raise HTTPException(404, "opportunity not found")


@router.post("/api/opportunities/{opportunity_id}/gate-a")
def opportunity_gate_a_run(opportunity_id: int):
    try:
        return evaluate_gate_a(opportunity_id)
    except KeyError:
        raise HTTPException(404, "opportunity not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
