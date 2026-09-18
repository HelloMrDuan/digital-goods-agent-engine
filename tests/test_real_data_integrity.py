from __future__ import annotations

from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import init_db
from app.services.analyze import _validate_product_draft
from app.services.ingest import insert_evidence
from app.services.opportunities import build_from_unlinked_evidence, list_opportunities
from app.services.quality import assert_real_opportunity_provenance, runtime_integrity_report
from app.services.sources import create_source


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    old_db = settings.db_path
    old_threshold = settings.cluster_similarity_threshold
    settings.db_path = str(tmp_path / "integrity.db")
    settings.cluster_similarity_threshold = 0.20
    init_db()
    yield
    settings.db_path = old_db
    settings.cluster_similarity_threshold = old_threshold


def test_runtime_integrity_rejects_fixture_provenance():
    sid = create_source({
        "name": "fixture-source",
        "source_type": "agent_reach",
        "platform": "reddit",
        "query": "template",
        "enabled": True,
        "config": {"role": "demand", "priority": "P0", "business_group": "core_demand"},
    })
    insert_evidence(
        {"id": sid},
        CollectedItem(
            platform="reddit",
            source_type="test",
            source_item_id="fixture-1",
            source_url="https://example.com/item/fixture-1",
            title="Need a payroll spreadsheet",
            content="I need a payroll spreadsheet template for overtime.",
            author="fixture-user",
            raw={"id": "fixture-1"},
        ),
    )

    report = runtime_integrity_report()

    assert report["status"] == "FAILED"
    assert report["real_data_only"] is False
    assert report["suspicious_evidence_count"] == 1


def test_nonproduction_evidence_cannot_reach_agent_analysis():
    sid = create_source({
        "name": "real-config-but-bad-row",
        "source_type": "agent_reach",
        "platform": "reddit",
        "query": "payroll",
        "enabled": True,
        "config": {"role": "demand", "priority": "P0", "business_group": "core_demand"},
    })
    insert_evidence(
        {"id": sid},
        CollectedItem(
            platform="reddit",
            source_type="fixture",
            source_item_id="bad-1",
            source_url="https://example.test/bad-1",
            title="Need payroll overtime Excel template",
            content="Need payroll overtime Excel template for a small business.",
            author="fixture-user",
            raw={"id": "bad-1"},
        ),
    )
    build_from_unlinked_evidence()
    opportunity_id = int(list_opportunities()[0]["id"])

    with pytest.raises(ValueError, match="non-production provenance"):
        assert_real_opportunity_provenance(opportunity_id)


def test_product_draft_missing_price_is_rejected_not_filled_with_zero():
    draft = {
        "name": "Payroll Workbook",
        "category": "office",
        "audience": "small teams",
        "title": "Payroll Workbook",
        "description": "Original workbook",
        "keywords": ["payroll"],
        "deliverables": ["xlsx"],
        "faq": [],
        "compliance_notes": "Original content only",
    }
    with pytest.raises(ValueError, match="price_suggestion"):
        _validate_product_draft(draft)


def test_complete_product_draft_keeps_explicit_price():
    draft = {
        "name": "Payroll Workbook",
        "category": "office",
        "audience": "small teams",
        "price_suggestion": 29.0,
        "title": "Payroll Workbook",
        "description": "Original workbook",
        "keywords": ["payroll"],
        "deliverables": ["xlsx"],
        "faq": [],
        "compliance_notes": "Original content only",
    }
    assert _validate_product_draft(draft)["price_suggestion"] == 29.0
