from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, dumps, init_db, now_iso
from app.services.ingest import insert_evidence
from app.services.opportunities import build_from_unlinked_evidence, list_opportunities
from app.services.product_lifecycle import (
    approve_product,
    evaluate_gate_b,
    get_gate_b,
    require_approved_current_product,
)
from app.services.sources import create_source
from app.services.validation import gate_a_from_validation, validate_opportunity


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
            "name": "reddit-product-gate",
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
            content=f"I need a payroll attendance spreadsheet that calculates overtime automatically. Case {item_id}.",
            author=author,
            published_at=(datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds"),
            engagement={"score": 3},
            raw={"id": item_id, "author": author},
        ),
    )


def _ready_opportunity() -> tuple[int, dict[str, int]]:
    source = _source()
    _insert(source, "one", "buyer-one")
    _insert(source, "two", "buyer-two", days_ago=2)
    build_from_unlinked_evidence()
    opportunity_id = int(list_opportunities()[0]["id"])
    assert validate_opportunity(opportunity_id)["status"] == "PASSED"
    with conn() as c:
        c.execute(
            """UPDATE opportunities SET status='ANALYZED',overall_score=88,copyright_risk=10,
                      analysis_json=?,analysis_stale=0,updated_at=? WHERE id=?""",
            (dumps({"decision": "BUILD", "final_score": 88}), now_iso(), opportunity_id),
        )
    return opportunity_id, source


def _product(opportunity_id: int, *, faq: bool = True, compliance: bool = True) -> int:
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO products(
                   opportunity_id,sku,name,category,audience,price_suggestion,title,description,
                   keywords_json,deliverables_json,faq_json,compliance_notes,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id,
                f"DG-{opportunity_id:06d}-TEST",
                "原创工资考勤表",
                "办公模板",
                "小团队",
                19.9,
                "原创工资考勤自动计算模板",
                "根据真实需求设计的原创表格模板。",
                dumps(["工资", "考勤"]),
                dumps(["原创 Excel 模板", "使用说明"]),
                dumps([{"q": "支持修改吗", "a": "支持按说明自行修改"}]) if faq else dumps([]),
                "仅交付原创模板，不包含第三方受版权保护资料。" if compliance else "",
                "DRAFT",
                now,
                now,
            ),
        )
    return int(cur.lastrowid)


def test_gate_a_maps_validation_states_without_inventing_metrics():
    assert gate_a_from_validation(
        {"opportunity_id": 1, "status": "PASSED", "is_current": True, "score": 90, "reason_codes": []}
    )["decision"] == "PASS"
    assert gate_a_from_validation(
        {"opportunity_id": 1, "status": "FAILED", "is_current": True, "reason_codes": ["no_explicit_demand"]}
    )["decision"] == "REJECT"
    assert gate_a_from_validation(
        {"opportunity_id": 1, "status": "FAILED", "is_current": True, "reason_codes": ["low_evidence_quality"]}
    )["decision"] == "REVIEW_REQUIRED"


def test_gate_b_passes_only_when_product_and_upstream_evidence_are_ready():
    opportunity_id, _ = _ready_opportunity()
    product_id = _product(opportunity_id)

    result = evaluate_gate_b(product_id)

    assert result["decision"] == "PASS"
    assert result["blockers"] == []
    assert result["review_flags"] == []
    assert get_gate_b(product_id)["is_current"] is True


def test_gate_b_requires_review_when_customer_service_or_compliance_metadata_missing():
    opportunity_id, _ = _ready_opportunity()
    product_id = _product(opportunity_id, faq=False, compliance=False)

    result = evaluate_gate_b(product_id)

    assert result["decision"] == "REVIEW_REQUIRED"
    assert "customer_service_knowledge_missing" in result["review_flags"]
    assert "compliance_notes_missing" in result["review_flags"]


def test_gate_b_becomes_stale_when_commercial_validation_snapshot_changes():
    opportunity_id, source = _ready_opportunity()
    product_id = _product(opportunity_id)
    assert evaluate_gate_b(product_id)["decision"] == "PASS"

    _insert(source, "three", "buyer-three", days_ago=3)
    build_from_unlinked_evidence()

    gate = get_gate_b(product_id)
    assert gate["decision"] == "PASS"
    assert gate["is_current"] is False


def test_approval_runs_gate_b_and_marks_opportunity_ready_to_publish():
    opportunity_id, _ = _ready_opportunity()
    product_id = _product(opportunity_id)

    result = approve_product(product_id)

    assert result["status"] == "APPROVED"
    assert result["gate_b"]["decision"] == "PASS"
    with conn() as c:
        product = c.execute("SELECT status,approved_at FROM products WHERE id=?", (product_id,)).fetchone()
        opportunity = c.execute("SELECT status FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    assert product["status"] == "APPROVED"
    assert product["approved_at"]
    assert opportunity["status"] == "READY_TO_PUBLISH"
    assert require_approved_current_product(product_id)["id"] == product_id


def test_approval_is_blocked_when_gate_b_needs_review():
    opportunity_id, _ = _ready_opportunity()
    product_id = _product(opportunity_id, faq=False)

    with pytest.raises(ValueError, match="Gate B must PASS"):
        approve_product(product_id)
