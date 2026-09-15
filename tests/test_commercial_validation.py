from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, init_db
from app.services.analyze import product_from_opportunity
from app.services.ingest import insert_evidence
from app.services.opportunities import build_from_unlinked_evidence, list_opportunities
from app.services.validation import get_validation, validate_opportunity
from app.services.sources import create_source


@pytest.fixture(autouse=True)
def isolated_db():
    old = {
        "db_path": settings.db_path,
        "cluster_similarity_threshold": settings.cluster_similarity_threshold,
        "commercial_validation_min_signals": settings.commercial_validation_min_signals,
        "commercial_validation_min_authors": settings.commercial_validation_min_authors,
        "commercial_validation_min_quality": settings.commercial_validation_min_quality,
        "commercial_validation_min_timestamp_coverage": settings.commercial_validation_min_timestamp_coverage,
        "commercial_validation_min_score": settings.commercial_validation_min_score,
    }
    with tempfile.TemporaryDirectory() as td:
        settings.db_path = str(Path(td) / "test.db")
        settings.cluster_similarity_threshold = 0.20
        settings.commercial_validation_min_signals = 2
        settings.commercial_validation_min_authors = 2
        settings.commercial_validation_min_quality = 55
        settings.commercial_validation_min_timestamp_coverage = 50
        settings.commercial_validation_min_score = 65
        init_db()
        yield
    for key, value in old.items():
        setattr(settings, key, value)


def _source() -> dict[str, int]:
    source_id = create_source(
        {
            "name": "reddit-core-demand",
            "source_type": "agent_reach",
            "platform": "reddit",
            "query": "payroll spreadsheet",
            "enabled": True,
            "config": {"role": "demand", "business_group": "core_demand", "priority": "P0"},
        }
    )
    return {"id": source_id}


def _insert(source: dict[str, int], item_id: str, author: str, days_ago: int = 1) -> None:
    insert_evidence(
        source,
        CollectedItem(
            platform="reddit",
            source_type="agent_reach_reddit",
            source_item_id=item_id,
            source_url=f"https://www.reddit.com/comments/{item_id}/",
            title="Need a payroll attendance spreadsheet template",
            content=f"I need a payroll attendance spreadsheet that calculates overtime automatically for a small team. Case {item_id}.",
            author=author,
            published_at=(datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds"),
            engagement={"score": 3},
            raw={"id": item_id, "author": author},
        ),
    )


def _opportunity_id() -> int:
    result = build_from_unlinked_evidence()
    assert result["candidate_groups"] == 1
    return int(list_opportunities()[0]["id"])


def test_single_signal_fails_commercial_validation():
    source = _source()
    _insert(source, "one", "buyer-one")
    opportunity_id = _opportunity_id()

    result = validate_opportunity(opportunity_id)

    assert result["status"] == "FAILED"
    assert "insufficient_independent_demand" in result["reason_codes"]
    assert "insufficient_author_or_platform_diversity" in result["reason_codes"]
    assert get_validation(opportunity_id)["is_current"] is True


def test_two_independent_real_demand_authors_pass_validation():
    source = _source()
    _insert(source, "one", "buyer-one")
    _insert(source, "two", "buyer-two", days_ago=2)
    opportunity_id = _opportunity_id()

    result = validate_opportunity(opportunity_id)

    assert result["status"] == "PASSED"
    assert result["metrics"]["effective_signal_count"] == 2
    assert result["metrics"]["unique_authors"] == 2
    assert result["metrics"]["explicit_demand_count"] == 2
    assert result["score"] >= settings.commercial_validation_min_score


def test_validation_becomes_stale_when_cluster_evidence_changes():
    source = _source()
    _insert(source, "one", "buyer-one")
    _insert(source, "two", "buyer-two", days_ago=2)
    opportunity_id = _opportunity_id()
    assert validate_opportunity(opportunity_id)["status"] == "PASSED"

    _insert(source, "three", "buyer-three", days_ago=3)
    assert _opportunity_id() == opportunity_id

    current = get_validation(opportunity_id)
    assert current["status"] == "STALE"
    assert current["is_current"] is False
    with conn() as c:
        assert c.execute("SELECT validation_status FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()["validation_status"] == "STALE"


def test_product_generation_requires_current_passed_validation(monkeypatch):
    source = _source()
    _insert(source, "one", "buyer-one")
    _insert(source, "two", "buyer-two", days_ago=2)
    opportunity_id = _opportunity_id()
    with conn() as c:
        c.execute(
            "UPDATE opportunities SET status='ANALYZED',overall_score=90,copyright_risk=10,analysis_json='{}',analysis_stale=0 WHERE id=?",
            (opportunity_id,),
        )

    called = False

    def fake_generate(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("app.services.analyze.generate_product", fake_generate)
    with pytest.raises(ValueError, match="commercial validation"):
        product_from_opportunity(opportunity_id)
    assert called is False

    assert validate_opportunity(opportunity_id)["status"] == "PASSED"
    result = product_from_opportunity(opportunity_id)
    assert called is True
    assert result["product_id"] > 0


def test_failed_validation_still_allows_discovery_agent(monkeypatch):
    from app.services.analyze import analyze_one

    source = _source()
    _insert(source, "only", "buyer-one")
    opportunity_id = _opportunity_id()
    assert validate_opportunity(opportunity_id)["status"] == "FAILED"
    called = False

    def fake_workflow(candidate_id: int):
        nonlocal called
        called = True
        assert candidate_id == opportunity_id
        return {
            "card_id": 7,
            "card": {
                "decision": "WATCH",
                "risk_score": 25,
                "final_score": 58,
                "commercial_validation": {"status": "FAILED"},
            },
        }

    monkeypatch.setattr("app.services.analyze.run_opportunity_agent_workflow", fake_workflow)
    result = analyze_one(opportunity_id)
    assert called is True
    assert result["card_id"] == 7
    assert result["analysis"]["decision"] == "WATCH"
    assert result["status"] == "ANALYZED"


def test_approved_product_is_blocked_after_validation_becomes_stale(monkeypatch):
    from fastapi import HTTPException
    from app.main import product_approve

    source = _source()
    _insert(source, "one", "buyer-one")
    _insert(source, "two", "buyer-two", days_ago=2)
    opportunity_id = _opportunity_id()
    validate_opportunity(opportunity_id)
    with conn() as c:
        c.execute(
            "UPDATE opportunities SET status='ANALYZED',overall_score=90,copyright_risk=10,analysis_json='{}',analysis_stale=0 WHERE id=?",
            (opportunity_id,),
        )
    monkeypatch.setattr("app.services.analyze.generate_product", lambda *args, **kwargs: {"name": "原创工资表"})
    product_id = product_from_opportunity(opportunity_id)["product_id"]

    _insert(source, "three", "buyer-three", days_ago=3)
    _opportunity_id()

    with pytest.raises(HTTPException) as caught:
        product_approve(product_id)
    assert caught.value.status_code == 400
    assert "commercial validation" in caught.value.detail


def test_existing_v1133_database_is_migrated_in_place():
    with conn() as c:
        c.execute("DROP TABLE commercial_validation_runs")
        for column in (
            "validation_json",
            "validated_at",
            "validation_snapshot_hash",
            "validation_score",
            "validation_status",
        ):
            c.execute(f"ALTER TABLE opportunities DROP COLUMN {column}")

    init_db()

    with conn() as c:
        columns = {row["name"] for row in c.execute("PRAGMA table_info(opportunities)").fetchall()}
        validation_table = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='commercial_validation_runs'"
        ).fetchone()
    assert {
        "validation_status",
        "validation_score",
        "validation_snapshot_hash",
        "validated_at",
        "validation_json",
    }.issubset(columns)
    assert validation_table is not None
