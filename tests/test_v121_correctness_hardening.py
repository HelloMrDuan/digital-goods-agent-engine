from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.db import conn, init_db, now_iso
from app.services import clustering, demand_discovery
from app import routes_v12


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    old = settings.db_path
    settings.db_path = str(tmp_path / "v121-correctness.db")
    init_db()
    yield
    settings.db_path = old


def test_all_demand_queries_failed_is_failed_not_empty(monkeypatch):
    demand_discovery.install_sources()
    probe = demand_discovery._PROBES[0]

    def fail_collect(*args, **kwargs):
        raise RuntimeError("collector offline")

    monkeypatch.setattr(demand_discovery, "collect_agent_reach", fail_collect)
    result = demand_discovery._collect_probe(probe, queries=("q1", "q2"))

    assert result["status"] == "FAILED"
    assert result["query_failures"] == 2
    with conn() as c:
        run = c.execute("SELECT status,error FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
        source = c.execute("SELECT status,failure_streak,empty_streak FROM sources WHERE name=?", (probe["name"],)).fetchone()
    assert run["status"] == "FAILED"
    assert "collector offline" in (run["error"] or "")
    assert source["status"] == "ERROR"
    assert int(source["failure_streak"]) == 1
    assert int(source["empty_streak"]) == 0


def test_cluster_rebuild_failure_keeps_previous_active_opportunity(monkeypatch):
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO opportunities(title,candidate_key,normalized_need,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("existing", "existing-key", "existing", "NEW", now, now),
        )
        opportunity_id = int(cur.lastrowid)
        c.execute(
            "UPDATE opportunities SET cluster_method='real_evidence_tfidf_v5_content_intent_gate',cluster_active=1 WHERE id=?",
            (opportunity_id,),
        )

    row = {"id": 101, "title": "candidate", "content": "need template", "platform": "reddit"}
    monkeypatch.setattr(clustering, "_load_candidates", lambda: ([row], [], {101: "demand"}, 1, 0))
    monkeypatch.setattr(clustering._legacy, "_cluster_rows", lambda rows: ([rows], {101: {}}, {}))
    monkeypatch.setattr(clustering._legacy, "_representative", lambda cluster, vectors: {"title": "candidate"})
    monkeypatch.setattr(clustering._legacy, "_signature", lambda cluster: "candidate-signature")
    monkeypatch.setattr(clustering._legacy, "_support_matches", lambda *args, **kwargs: [])

    def fail_during_transaction(*args, **kwargs):
        raise RuntimeError("rebuild exploded after active switch started")

    monkeypatch.setattr(clustering, "_existing_for_cluster", fail_during_transaction)
    with pytest.raises(RuntimeError, match="rebuild exploded"):
        clustering.rebuild_clusters()

    with conn() as c:
        row = c.execute("SELECT cluster_active FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
        run = c.execute("SELECT status FROM cluster_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert int(row["cluster_active"]) == 1
    assert run["status"] == "FAILED"


def test_rebuild_rejects_duplicate_active_task():
    routes_v12._ensure_rebuild_schema()
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO opportunity_rebuild_tasks(
                   status,progress,stage,message,analyze_enabled,refresh_enabled,requested_limit,created_at,updated_at
               ) VALUES('RUNNING',25,'Agent','working',1,1,8,?,?)""",
            (now, now),
        )
        active_id = int(cur.lastrowid)

    with pytest.raises(HTTPException) as exc:
        routes_v12.opportunities_rebuild(
            analyze=False,
            refresh=False,
            limit=8,
            refresh_queries_per_probe=1,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["task_id"] == active_id

    with conn() as c:
        count = c.execute(
            "SELECT COUNT(*) c FROM opportunity_rebuild_tasks WHERE status IN ('PENDING','RUNNING')"
        ).fetchone()
    assert int(count["c"]) == 1


def test_single_and_batch_analysis_share_same_analyze_one_entrypoint(monkeypatch):
    calls: list[int] = []

    def fake_analyze(opportunity_id: int):
        calls.append(opportunity_id)
        return {
            "id": opportunity_id,
            "status": "ANALYZED",
            "analysis": {
                "commercial_validation": {"status": "FAILED"},
                "decision": "WATCH",
                "decision_guard": "commercial_validation_not_passed",
            },
        }

    import app.main_legacy as main_legacy

    monkeypatch.setattr(main_legacy, "analyze_one", fake_analyze)
    single = main_legacy.opportunity_analyze(7)
    assert single["analysis"]["decision"] == "WATCH"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn() as c:
        c.execute(
            "INSERT INTO opportunities(id,title,candidate_key,normalized_need,status,evidence_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (8, "batch", "batch-key", "batch", "NEW", 1, now, now),
        )
        c.execute("UPDATE opportunities SET cluster_active=1,analysis_stale=0 WHERE id=8")
    batch = main_legacy.opportunities_analyze_new()
    assert any(item.get("id") == 8 and item["analysis"]["decision"] == "WATCH" for item in batch["results"])
    assert 7 in calls and 8 in calls



def test_async_single_analysis_does_not_require_commercial_validation_and_deduplicates(monkeypatch):
    import app.main_legacy as main_legacy

    now = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO opportunities(title,candidate_key,normalized_need,status,evidence_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("weak candidate", "weak-start-key", "weak", "NEW", 1, now, now),
        )
        opportunity_id = int(cur.lastrowid)
        c.execute("UPDATE opportunities SET cluster_active=1 WHERE id=?", (opportunity_id,))

    started: list[tuple] = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(main_legacy.threading, "Thread", FakeThread)

    first = main_legacy.opportunities_analyze_start(opportunity_id)
    assert int(first["task_id"]) > 0
    assert len(started) == 1

    with pytest.raises(HTTPException) as exc:
        main_legacy.opportunities_analyze_start(opportunity_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["task_id"] == first["task_id"]

    with conn() as c:
        tasks = c.execute(
            "SELECT COUNT(*) c FROM analysis_tasks WHERE opportunity_id=? AND status IN ('PENDING','RUNNING')",
            (opportunity_id,),
        ).fetchone()
    assert int(tasks["c"]) == 1
