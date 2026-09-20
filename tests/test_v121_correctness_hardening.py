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



def test_rebuild_gate_a_filters_context_and_non_pass_candidates(monkeypatch):
    now = now_iso()
    with conn() as c:
        direct_pass = c.execute(
            """INSERT INTO opportunities(
                   title,candidate_key,normalized_need,status,evidence_count,created_at,updated_at,
                   cluster_active,last_seen_at,explicit_demand_count,probable_demand_count,effective_signal_count
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("direct pass", "direct-pass", "direct pass", "NEW", 2, now, now, 1, now, 2, 0, 2),
        ).lastrowid
        direct_fail = c.execute(
            """INSERT INTO opportunities(
                   title,candidate_key,normalized_need,status,evidence_count,created_at,updated_at,
                   cluster_active,last_seen_at,explicit_demand_count,probable_demand_count,effective_signal_count
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("direct fail", "direct-fail", "direct fail", "NEW", 1, now, now, 1, now, 1, 0, 1),
        ).lastrowid
        c.execute(
            """INSERT INTO opportunities(
                   title,candidate_key,normalized_need,status,evidence_count,created_at,updated_at,
                   cluster_active,last_seen_at,explicit_demand_count,probable_demand_count,effective_signal_count
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("context only", "context-only", "context only", "NEW", 5, now, now, 1, now, 0, 0, 0),
        )

    calls: list[int] = []

    def fake_gate(opportunity_id: int):
        calls.append(opportunity_id)
        if opportunity_id == int(direct_pass):
            return {"decision": "PASS", "score": 88, "reasons": []}
        return {"decision": "REJECT", "score": 30, "reasons": ["insufficient_independent_demand"]}

    monkeypatch.setattr(routes_v12, "evaluate_gate_a", fake_gate)
    result = routes_v12._validate_recent_demand_candidates()

    assert result["processed"] == 2
    assert result["passed"] == 1
    assert result["rejected"] == 1
    assert result["passed_ids"] == [int(direct_pass)]
    assert int(direct_pass) in calls
    assert int(direct_fail) in calls
    assert len(calls) == 2


def test_rebuild_without_ai_still_runs_gate_a(monkeypatch):
    updates: list[dict] = []

    monkeypatch.setattr(routes_v12, "run_demand_discovery", lambda **kwargs: {
        "retained_recent": 4,
        "direct_demand_signals": 3,
    })
    monkeypatch.setattr(routes_v12, "build_from_unlinked_evidence", lambda: {
        "candidate_groups": 2,
        "processed_evidence": 3,
    })
    monkeypatch.setattr(routes_v12, "_validate_recent_demand_candidates", lambda limit=100: {
        "processed": 2,
        "passed": 1,
        "review_required": 0,
        "rejected": 1,
        "errors": 0,
        "passed_ids": [99],
        "results": [],
        "policy": "test",
    })
    monkeypatch.setattr(routes_v12, "_update_rebuild", lambda task_id, **values: updates.append(values))

    routes_v12._run_rebuild(
        task_id=123,
        analyze_enabled=False,
        limit=8,
        refresh_enabled=True,
        refresh_queries_per_probe=2,
    )

    final = updates[-1]
    assert final["status"] == "SUCCESS"
    payload = routes_v12.loads(final["result_json"], {})
    assert payload["gate_a"]["passed"] == 1
    assert payload["agent_analysis"] == []
    assert "PASS 1/2" in final["message"]
