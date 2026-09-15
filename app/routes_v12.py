from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .config import settings
from .db import conn, dumps, loads, now_iso, row_to_dict
from .services.analyze import analyze_one
from .services.opportunities import build_from_unlinked_evidence, get_evidence_for_opportunity, get_opportunity
from .services.opportunity_agent import (
    get_card_for_opportunity,
    get_opportunity_card,
    list_agent_runs,
    list_opportunity_cards,
)

router = APIRouter()


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


def _run_rebuild(task_id: int, analyze_enabled: bool, limit: int) -> None:
    results: list[dict[str, Any]] = []
    try:
        _update_rebuild(task_id, status="RUNNING", progress=5, stage="重建候选", message="正在从现有真实 evidence 重建商机候选")
        cluster_result = build_from_unlinked_evidence()
        if not analyze_enabled:
            payload = {"cluster_run": cluster_result, "agent_analysis": []}
            _update_rebuild(
                task_id,
                status="SUCCESS",
                progress=100,
                stage="完成",
                message="候选重建完成；未执行付费 AI",
                result_json=dumps(payload),
            )
            return

        with conn() as c:
            rows = c.execute(
                """SELECT id FROM opportunities
                   WHERE cluster_active=1
                   ORDER BY coalesce(analysis_stale,0) DESC,
                            coalesce(trusted_trend_score,0) DESC,
                            coalesce(data_quality_score,0) DESC,
                            evidence_count DESC,id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        ids = [int(row["id"]) for row in rows]
        total = len(ids)
        _update_rebuild(task_id, progress=12, stage="准备 Agent", message=f"待分析 {total} 个商机", total=total)

        succeeded = 0
        failed = 0
        for index, opportunity_id in enumerate(ids, start=1):
            progress = 12 + int((index - 1) / max(1, total) * 83)
            _update_rebuild(
                task_id,
                progress=progress,
                stage=f"多 Agent {index}/{total}",
                message=f"正在分析商机 #{opportunity_id}",
                current_opportunity_id=opportunity_id,
                processed=index - 1,
                succeeded=succeeded,
                failed=failed,
            )
            analysis_task_id = _analysis_task_start(opportunity_id)
            try:
                result = analyze_one(opportunity_id)
                succeeded += 1
                results.append({"opportunity_id": opportunity_id, "status": "SUCCESS", "result": result})
                _analysis_task_finish(analysis_task_id, result=result)
            except Exception as exc:
                failed += 1
                message = str(exc)
                results.append({"opportunity_id": opportunity_id, "status": "FAILED", "error": message})
                _analysis_task_finish(analysis_task_id, error=message)
            _update_rebuild(task_id, processed=index, succeeded=succeeded, failed=failed)

        if total == 0 or failed == 0:
            status = "SUCCESS"
        elif succeeded > 0:
            status = "PARTIAL"
        else:
            status = "FAILED"
        payload = {"cluster_run": cluster_result, "agent_analysis": results}
        _update_rebuild(
            task_id,
            status=status,
            progress=100,
            stage="完成" if status != "FAILED" else "失败",
            message=f"处理 {total} 个商机：成功 {succeeded}，失败 {failed}",
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
            result_json=dumps({"agent_analysis": results}),
        )


def _rebuild_task_payload(task_id: int) -> dict[str, Any]:
    _ensure_rebuild_schema()
    with conn() as c:
        row = c.execute("SELECT * FROM opportunity_rebuild_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise KeyError("rebuild task not found")
    data = row_to_dict(row) or {}
    data["analyze_enabled"] = bool(data.get("analyze_enabled"))
    data["result"] = loads(data.pop("result_json", None), None)
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
    limit: int = Query(default=8, ge=1, le=20),
):
    _ensure_rebuild_schema()
    if analyze and not settings.is_llm_configured:
        raise HTTPException(409, "LLM is not configured; set Qiniu or SenseNova, or call with analyze=false")
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO opportunity_rebuild_tasks(
                   status,progress,stage,message,analyze_enabled,requested_limit,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("PENDING", 0, "排队", "等待重建", 1 if analyze else 0, limit, now, now),
        )
    task_id = int(cur.lastrowid)
    threading.Thread(target=_run_rebuild, args=(task_id, analyze, limit), daemon=True).start()
    return {"task_id": task_id, "status": "PENDING", "analyze": analyze, "limit": limit}


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
