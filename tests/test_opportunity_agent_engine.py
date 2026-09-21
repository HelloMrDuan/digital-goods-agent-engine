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
    payload = kwargs.get("payload") or {}
    digest = payload.get("digest") or {}
    refs = list((digest.get("real_metrics") or {}).get("evidence_ids") or [])
    if not refs:
        refs = list(((payload.get("digest_metrics") or {}).get("evidence_ids") or []))
    if not refs and isinstance(payload.get("signal"), dict):
        refs = list(payload["signal"].get("evidence_refs") or [])
    refs = refs[:2]
    outputs = {
        "signal": {
            "signals": ["repeated request for payroll automation"],
            "signal_strength": 82,
            "demand_pattern": "small teams want payroll automation",
            "evidence_refs": refs,
            "confidence": 85,
            "reasoning": "grounded in supplied requests",
        },
        "pain": {
            "target_users": ["small business operators"],
            "pain_points": [{"pain": "manual overtime calculation", "severity": 80, "evidence_refs": refs}],
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
    from app.services.analyze import analyze_one

    opportunity_id = _opportunity_id(1)
    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", _fake_chat)

    result = analyze_one(opportunity_id)

    assert result["analysis"]["commercial_validation"]["status"] == "FAILED"
    # Discovery AI can still inspect weak real evidence, but a failed deterministic Gate
    # must never surface BUILD to the operator.
    assert result["analysis"]["decision"] == "WATCH"
    assert result["analysis"]["decision_guard"] == "commercial_validation_not_passed"
    card = opportunity_agent.get_card_for_opportunity(opportunity_id)
    assert card is not None
    assert card["decision"] == "WATCH"
    assert card["card"]["decision_guard"] == "commercial_validation_not_passed"
    with conn() as c:
        row = c.execute("SELECT COUNT(*) count FROM agent_runs WHERE opportunity_id=?", (opportunity_id,)).fetchone()
    assert int(row["count"]) == 6



def test_agent_schema_rejects_evidence_refs_not_present_in_input(monkeypatch):
    opportunity_id = _opportunity_id(2)

    def fake_bad_ref(**kwargs):
        output, meta = _fake_chat(**kwargs)
        if kwargs["operation"] == "opportunity_agent_signal":
            output = dict(output)
            output["evidence_refs"] = [999999]
        return output, meta

    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", fake_bad_ref)

    with pytest.raises(ValueError, match="evidence_refs outside input evidence"):
        opportunity_agent.run_opportunity_agent_workflow(opportunity_id)

    runs = opportunity_agent.list_agent_runs(opportunity_id)
    assert len(runs) == 1
    assert runs[0]["agent_name"] == "signal"
    assert runs[0]["status"] == "FAILED"


def test_agent_schema_rejects_extra_fields(monkeypatch):
    opportunity_id = _opportunity_id(2)

    def fake_extra(**kwargs):
        output, meta = _fake_chat(**kwargs)
        if kwargs["operation"] == "opportunity_agent_signal":
            output = dict(output)
            output["invented_metric"] = 123
        return output, meta

    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", fake_extra)

    with pytest.raises(ValueError, match="output schema invalid"):
        opportunity_agent.run_opportunity_agent_workflow(opportunity_id)



def test_market_intel_reaches_market_and_judge_agents(monkeypatch):
    opportunity_id = _opportunity_id(2)
    captured = {}

    monkeypatch.setattr(
        opportunity_agent,
        "market_intel_for_opportunity",
        lambda oid: {
            "matched_count": 1,
            "high_heat_count": 1,
            "high_sales_count": 1,
            "hot_and_selling_count": 1,
            "max_heat_score": 88.0,
            "max_sales_score": 72.0,
            "items": [{
                "id": 99,
                "title": "Payroll Workbook",
                "rating": 4.9,
                "review_count": 400,
                "sales_count": 12000,
                "sales_scope": "shop",
                "sales_verified": True,
                "heat_score": 88.0,
                "sales_score": 72.0,
                "market_score": 80.8,
            }],
            "truth_policy": "real metrics only",
        },
    )

    def recording_chat(**kwargs):
        name = str(kwargs["operation"]).replace("opportunity_agent_", "")
        if name in {"market", "judge"}:
            captured[name] = kwargs.get("payload") or {}
        return _fake_chat(**kwargs)

    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", recording_chat)
    opportunity_agent.run_opportunity_agent_workflow(opportunity_id)

    assert captured["market"]["market_intel"]["items"][0]["rating"] == 4.9
    assert captured["market"]["market_intel"]["items"][0]["sales_scope"] == "shop"
    assert captured["judge"]["market_intel"]["items"][0]["sales_verified"] is True



@pytest.mark.parametrize(
    "structured_signals",
    [
        [
            {
                "type": "explicit_demand",
                "topic": "PDF to Excel Conversion with OCR and Image Preservation",
                "specifics": "Automation for converting MCQ PDFs into custom Excel formats without altering content.",
            },
            {
                "type": "explicit_demand",
                "topic": "Clay to Cold Email Automation",
                "specifics": "Eliminating manual CSV export/upload steps between enrichment and sending tools.",
            },
        ],
        [
            {
                "type": "explicit_demand",
                "topic": "Affordable Invoice and Contract Automation for Small Business",
                "description": "Small business owners are explicitly seeking affordable paperwork automation.",
            },
            {
                "type": "market_sentiment",
                "topic": "Practical Utility Over Hyped AI Agents",
                "description": "Users want boring but useful automation rather than flashy demos.",
            },
        ],
    ],
)
def test_signal_schema_normalizes_supported_structured_model_output(structured_signals):
    output = {
        "signals": structured_signals,
        "signal_strength": 82,
        "demand_pattern": "repeated practical automation requests",
        "evidence_refs": [101, 102],
        "confidence": 84,
        "reasoning": "grounded in supplied evidence",
    }

    value = opportunity_agent._validate_agent_output("signal", output, {101, 102})

    assert len(value["signals"]) == 2
    assert all(isinstance(item, str) and item for item in value["signals"])
    assert "explicit_demand" in value["signals"][0]


def test_signal_schema_still_rejects_arbitrary_structured_fields():
    output = {
        "signals": [
            {
                "type": "explicit_demand",
                "topic": "Payroll automation",
                "specifics": "Need a payroll workbook.",
                "invented_metric": 999,
            }
        ],
        "signal_strength": 82,
        "demand_pattern": "payroll",
        "evidence_refs": [101],
        "confidence": 84,
        "reasoning": "grounded",
    }

    with pytest.raises(ValueError, match="unsupported fields"):
        opportunity_agent._validate_agent_output("signal", output, {101})



def test_product_schema_normalizes_string_deliverables_from_model():
    output = {
        "recommended_products": [
            {
                "name": "OCR Import Workbook",
                "format": "xlsx",
                "why": "Matches the validated workflow.",
                "deliverables": "A robust .xlsx file with protected headers and validation lists.",
            },
            {
                "name": "OCR Helper Script",
                "format": "python",
                "why": "Automates preprocessing.",
                "deliverables": ["main.py", "README.md"],
            },
        ],
        "primary_product": "OCR Import Workbook",
        "price_range": {"currency": "USD", "min": 19, "max": 49, "reason": "test"},
        "production_complexity": 40,
        "automation_fit": 90,
        "confidence": 80,
        "reasoning": "grounded",
    }

    value = opportunity_agent._validate_agent_output("product", output, {101})

    assert value["recommended_products"][0]["deliverables"] == [
        "A robust .xlsx file with protected headers and validation lists."
    ]
    assert value["recommended_products"][1]["deliverables"] == ["main.py", "README.md"]


def test_product_schema_still_rejects_non_string_non_list_deliverables():
    output = {
        "recommended_products": [
            {
                "name": "Bad Product",
                "format": "xlsx",
                "why": "test",
                "deliverables": {"file": "bad.xlsx"},
            }
        ],
        "primary_product": "Bad Product",
        "price_range": {"currency": "USD", "min": 1, "max": 2, "reason": "test"},
        "production_complexity": 10,
        "automation_fit": 90,
        "confidence": 80,
        "reasoning": "grounded",
    }

    with pytest.raises(ValueError, match="product agent output schema invalid"):
        opportunity_agent._validate_agent_output("product", output, {101})



def test_market_schema_normalizes_structured_buyer_profiles_from_model():
    output = {
        "category": "Automation Services",
        "market_score": 78,
        "purchase_intent": 82,
        "competition_level": 44,
        "competition_gaps": ["clear implementation roadmap"],
        "buyer_profile": [
            {
                "persona": "The Pre-Launch Solo Operator",
                "characteristics": "Technical enough to build stacks but anxious about client acquisition and marketing strategy.",
                "urgency_driver": "Resolve strategic doubts before activating campaigns and generating first revenue.",
            },
            {
                "persona": "The Efficiency-Seeking Agency Owner",
                "characteristics": "Uses generic AI tools but wants dedicated automation agents to scale content production.",
                "urgency_driver": "Replace semi-manual workflows as client volume grows.",
            },
        ],
        "confidence": 84,
        "reasoning": "grounded in supplied evidence",
    }

    value = opportunity_agent._validate_agent_output("market", output, {101})

    assert len(value["buyer_profile"]) == 2
    assert value["buyer_profile"][0].startswith("The Pre-Launch Solo Operator")
    assert "Urgency:" in value["buyer_profile"][0]
    assert all(isinstance(item, str) for item in value["buyer_profile"])


def test_market_schema_rejects_unknown_buyer_profile_object_fields():
    output = {
        "category": "Automation Services",
        "market_score": 78,
        "purchase_intent": 82,
        "competition_level": 44,
        "competition_gaps": ["clear implementation roadmap"],
        "buyer_profile": [
            {
                "persona": "Buyer",
                "characteristics": "Needs automation.",
                "invented_score": 99,
            }
        ],
        "confidence": 84,
        "reasoning": "grounded",
    }

    with pytest.raises(ValueError, match="unsupported fields"):
        opportunity_agent._validate_agent_output("market", output, {101})



def test_build_readiness_explains_exact_build_gaps():
    readiness = opportunity_agent._build_readiness(
        final_score=68,
        market_score=72,
        risk_score=38,
        validation={"status": "PASSED"},
    )

    assert readiness["eligible"] is False
    assert readiness["gaps"] == [
        {
            "code": "final_score_below_build_threshold",
            "actual": 68,
            "required": float(settings.agent_build_score),
            "shortfall": float(settings.agent_build_score) - 68,
        }
    ]


def test_card_list_defaults_to_current_active_non_stale_only(monkeypatch):
    opportunity_id = _opportunity_id(2)
    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", _fake_chat)
    opportunity_agent.run_opportunity_agent_workflow(opportunity_id)

    current = opportunity_agent.list_opportunity_cards()
    assert [int(card["opportunity_id"]) for card in current] == [opportunity_id]
    assert current[0]["build_readiness"]["eligible"] is True

    with conn() as c:
        c.execute("UPDATE opportunities SET cluster_active=0 WHERE id=?", (opportunity_id,))

    assert opportunity_agent.list_opportunity_cards() == []
    history = opportunity_agent.list_opportunity_cards(current_only=False)
    assert [int(card["opportunity_id"]) for card in history] == [opportunity_id]



def test_card_list_excludes_snapshot_mismatch_even_when_flags_look_current(monkeypatch):
    opportunity_id = _opportunity_id(2)
    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", _fake_chat)
    result = opportunity_agent.run_opportunity_agent_workflow(opportunity_id)
    assert result["card"]["snapshot_hash"]

    with conn() as c:
        c.execute(
            "UPDATE opportunity_cards SET snapshot_hash='stale-snapshot' WHERE opportunity_id=?",
            (opportunity_id,),
        )
        c.execute(
            "UPDATE opportunities SET cluster_active=1,analysis_stale=0 WHERE id=?",
            (opportunity_id,),
        )

    assert opportunity_agent.list_opportunity_cards() == []

    history = opportunity_agent.list_opportunity_cards(current_only=False)
    assert len(history) == 1
    assert history[0]["snapshot_current"] is False
    assert history[0]["current_snapshot_hash"]



def test_primary_product_view_exposes_only_selected_launch_candidate():
    product = {
        "recommended_products": [
            {
                "name": "Low Risk Guide",
                "format": "markdown",
                "why": "evidence backed",
                "deliverables": ["guide.md"],
            },
            {
                "name": "Legal Kit",
                "format": "txt",
                "why": "secondary only",
                "deliverables": ["msa.txt", "privacy_policy.txt"],
            },
        ],
        "primary_product": "Low Risk Guide",
        "price_range": {"currency": "USD", "min": 19, "max": 39, "reason": "test"},
        "production_complexity": 20,
        "automation_fit": 95,
        "confidence": 88,
        "reasoning": "test",
    }

    view = opportunity_agent._primary_product_view(product)

    assert view["product"]["name"] == "Low Risk Guide"
    assert "recommended_products" not in view
    assert "Legal Kit" not in str(view)


def test_risk_and_judge_receive_primary_product_not_full_alternatives(monkeypatch):
    opportunity_id = _opportunity_id(2)
    captured = {}

    def recording_chat(**kwargs):
        name = str(kwargs["operation"]).replace("opportunity_agent_", "")
        if name in {"risk", "judge"}:
            captured[name] = kwargs.get("payload") or {}
        return _fake_chat(**kwargs)

    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", recording_chat)
    opportunity_agent.run_opportunity_agent_workflow(opportunity_id)

    for name in ("risk", "judge"):
        assert captured[name]["primary_product"]["product"]["name"] == "Payroll Overtime Workbook"
        assert "product" not in captured[name]
        assert "recommended_products" not in captured[name]["primary_product"]



def test_market_prompt_forbids_zero_market_intel_blue_ocean_inference():
    prompt = opportunity_agent._AGENT_SYSTEM["market"]
    assert "matched_count=0" in prompt
    assert "不能据此推断低竞争" in prompt
    assert "蓝海" in prompt


def test_judge_payload_includes_deterministic_decision_policy(monkeypatch):
    opportunity_id = _opportunity_id(2)
    captured = {}

    def recording_chat(**kwargs):
        name = str(kwargs["operation"]).replace("opportunity_agent_", "")
        if name == "judge":
            captured["judge"] = kwargs.get("payload") or {}
        return _fake_chat(**kwargs)

    monkeypatch.setattr(opportunity_agent, "chat_json_with_meta", recording_chat)
    opportunity_agent.run_opportunity_agent_workflow(opportunity_id)

    policy = captured["judge"]["decision_policy"]
    assert policy["build_if_final_gte"] == float(settings.agent_build_score)
    assert policy["build_if_market_gte"] == float(settings.agent_build_market_score)
    assert policy["watch_if_final_gte"] == float(settings.agent_watch_score)
    assert policy["drop_if_risk_gte"] == float(settings.high_risk_threshold)


def test_decision_consistency_flags_judge_label_score_mismatch():
    value = opportunity_agent._decision_consistency(
        "WATCH",
        final_score=48,
        market_score=72,
        risk_score=42,
    )
    assert value == {
        "proposed": "WATCH",
        "deterministic": "DROP",
        "matches_policy": False,
    }
