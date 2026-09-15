from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, init_db
from app.services import opportunity_agent
from app.services.ingest import insert_evidence
from app.services.llm.qiniu import _extract_json
from app.services.opportunities import build_from_unlinked_evidence, list_opportunities
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
        "min_evidence_for_analysis": settings.min_evidence_for_analysis,
    }
    with tempfile.TemporaryDirectory() as td:
        settings.db_path = str(Path(td) / "test.db")
        settings.cluster_similarity_threshold = 0.20
        settings.commercial_validation_min_signals = 2
        settings.commercial_validation_min_authors = 2
        settings.commercial_validation_min_quality = 55
        settings.commercial_validation_min_timestamp_coverage = 50
        settings.commercial_validation_min_score = 65
        settings.min_evidence_for_analysis = 1
        init_db()
        yield
    for key, value in old.items():
        setattr(settings, key, value)


def _source() -> int:
    return create_source(
        {
            "name": "reddit-agent-test",
            "source_type": "agent_reach",
            "platform": "reddit",
            "query": "small business spreadsheet",
            "enabled": True,
            "config": {"role": "demand", "business_group": "core_demand", "priority": "P0"},
        }
    )


def _insert(source_id: int, item_id: str, author: str) -> None:
    insert_evidence(
        {"id": source_id},
        CollectedItem(
            platform="reddit",
            source_type="agent_reach_reddit",
            source_item_id=item_id,
            source_url=f"https://www.reddit.com/comments/{item_id}/",
            title="Need a spreadsheet to automate payroll and overtime",
            content="I need a spreadsheet template that automatically calculates overtime and payroll for my small team.",
            author=author,
            published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            engagement={"score": 2},
            raw={"id": item_id, "author": author},
        ),
    )


def _opportunity_id(signal_count: int = 2) -> int:
    source_id = _source()
    for idx in range(signal_count):
        _insert(source_id, f"item-{idx}", f"buyer-{idx}")
    build_from_unlinked_evidence()
    rows = list_opportunities()
    assert rows
    return int(rows[0]["id"])


def _fake_chat(**kwargs):
    name = str(kwargs["operation"]).replace("opportunity_agent_", "")
    outputs = {
        "signal": {
            "signals": ["repeated request for payroll automation"],
            "signal_strength": 82,
            "demand_pattern": "small teams want payroll automation",
            "evidence_refs": [1, 2],
            "confidence": 85,
            "reasoning": "grounded in supplied requests",
        },
        "pain": {
            "target_users": ["small business operators"],
            "pain_points": [{"pain": "manual overtime calculation", "severity": 80, "evidence_refs": [1, 2]}],
            "urgency": 72,
            "confidence": 82,
            "reasoning": "manual payroll work is repeatedly requested",
        },
        "market": {
            "category": "office automation",
            "market_score": 78,
            "purchase_intent": 70,
            "competition_level": 45,
            "competition_gaps": ["simple localized workflow"],
            "buyer_profile": ["small team managers"],
            "confidence": 76,
            "reasoning": "current evidence supports a practical niche",
        },
        "product": {
            "recommended_products": [
                {"name": "Payroll Overtime Workbook", "format": "xlsx", "why": "matches the pain", "deliverables": ["workbook", "guide"]}
            ],
            "primary_product": "Payroll Overtime Workbook",
            "price_range": {"currency": "CNY", "min": 29, "max": 69, "reason": "AI judgement only"},
            "production_complexity": 35,
            "automation_fit": 88,
            "confidence": 80,
            "reasoning": "can be delivered automatically",
        },
        "risk": {
            "risk_score": 22,
            "copyright_risk": 10,
            "saturation_risk": 35,
            "payment_risk": 30,
            "platform_risks": [],
            "warnings": ["validate formulas before sale"],
            "blockers": [],
            "confidence": 84,
            "reasoning": "original spreadsheet has low copyright risk",
        },
        "judge": {
            "final_score": 81,
            "decision": "BUILD",
            "confidence": 83,
            "title": "Small-team payroll automation workbook",
            "category": "office automation",
            "summary": "evidence-backed candidate",
            "reasoning": "multiple real requests and manageable risk",
        },
    }
    return outputs[name], {
        "provider": "Qiniu",
        "model": "qwen/qwen3.5-plus",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_qiniu_json_parser_accepts_markdown_fence():
    assert _extract_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_multi_agent_workflow_persists_card_and_six_runs(monkeypatch):
    opportunity_id = _opportunity_id(2)
    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", _fake_chat)

    result = opportunity_agent.run_opportunity_agent_workflow(opportunity_id)

    assert result["card"]["decision"] == "BUILD"
    assert result["card"]["primary_product"] == "Payroll Overtime Workbook"
    card = opportunity_agent.get_card_for_opportunity(opportunity_id)
    assert card is not None
    assert card["decision"] == "BUILD"
    runs = opportunity_agent.list_agent_runs(opportunity_id)
    assert len(runs) == 6
    assert {row["agent_name"] for row in runs} == set(opportunity_agent.AGENT_ORDER)
    assert all(row["status"] == "SUCCESS" for row in runs)


def test_failed_deterministic_validation_no_longer_blocks_discovery_agent(monkeypatch):
    opportunity_id = _opportunity_id(1)
    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", _fake_chat)

    result = opportunity_agent.run_opportunity_agent_workflow(opportunity_id)

    assert result["card"]["commercial_validation"]["status"] == "FAILED"
    assert result["card"]["decision"] in {"BUILD", "WATCH", "DROP"}
    with conn() as c:
        row = c.execute("SELECT COUNT(*) count FROM agent_runs WHERE opportunity_id=?", (opportunity_id,)).fetchone()
    assert int(row["count"]) == 6
