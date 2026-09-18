from __future__ import annotations

import json
import secrets
from typing import Any

from ..config import settings
from ..db import conn, dumps, now_iso
from .opportunities import get_evidence_for_opportunity, get_opportunity
from .llm import chat_json as provider_chat_json
from .opportunity_agent import run_opportunity_agent_workflow
from .sensenova import generate_product
from .validation import require_current_validation


def _apply_commercial_decision_guard(opportunity_id: int, result: dict[str, Any]) -> dict[str, Any]:
    """Never expose BUILD unless the deterministic current-snapshot Gate passed.

    Discovery Agents are intentionally allowed to inspect weak real evidence, but the operator-facing
    Opportunity Card must not contradict the product-generation Gate.
    """
    card = result.get("card") or {}
    validation = card.get("commercial_validation") or {}
    validation_passed = str(validation.get("status") or "").upper() == "PASSED"
    card["decision_guard"] = (
        "commercial_validation_passed" if validation_passed else "commercial_validation_not_passed"
    )
    if not validation_passed and str(card.get("decision") or "").upper() == "BUILD":
        final_score = float(card.get("final_score") or 0)
        risk_score = float(card.get("risk_score") or 0)
        card["decision"] = (
            "WATCH"
            if risk_score < settings.high_risk_threshold and final_score >= settings.agent_watch_score
            else "DROP"
        )
        blockers = list(card.get("blockers") or [])
        if "commercial_validation_not_passed" not in blockers:
            blockers.append("commercial_validation_not_passed")
        card["blockers"] = blockers

    now = now_iso()
    card_id = result.get("card_id")
    with conn() as c:
        card_table = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_cards'"
        ).fetchone()
        if card_id and card_table:
            c.execute(
                "UPDATE opportunity_cards SET decision=?,card_json=?,updated_at=? WHERE id=?",
                (card.get("decision"), dumps(card), now, int(card_id)),
            )
        c.execute(
            "UPDATE opportunities SET analysis_json=?,updated_at=? WHERE id=?",
            (dumps(card), now, opportunity_id),
        )
    result["card"] = card
    return card


def analyze_one(opportunity_id: int) -> dict[str, Any]:
    """Run the V1.2 evidence-first multi-Agent workflow.

    Commercial validation is still calculated and included in the card, but it no longer
    blocks the analysis itself. This lets Signal/Pain/Market agents inspect a real evidence
    candidate and safely return WATCH/DROP instead of silently discarding it before AI.
    Product generation remains protected by the deterministic validation gate below.
    """
    result = run_opportunity_agent_workflow(opportunity_id)
    card = _apply_commercial_decision_guard(opportunity_id, result)
    risk_score = float(card.get("risk_score") or 0)
    status = "REJECTED_RISK" if risk_score >= settings.high_risk_threshold else "ANALYZED"
    return {
        "id": opportunity_id,
        "status": status,
        "analysis": card,
        "card_id": result.get("card_id"),
    }



def _validate_product_draft(product: dict[str, Any]) -> dict[str, Any]:
    """Reject incomplete AI drafts instead of filling missing business fields with fake defaults."""
    required_text = ("name", "category", "audience", "title", "description", "compliance_notes")
    for field in required_text:
        value = product.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"product draft missing required field: {field}")
    price = product.get("price_suggestion")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or float(price) < 0:
        raise ValueError("product draft price_suggestion must be a non-negative number")
    for field in ("keywords", "deliverables", "faq"):
        if not isinstance(product.get(field), list):
            raise ValueError(f"product draft {field} must be an array")
    if not product["deliverables"]:
        raise ValueError("product draft deliverables must not be empty")
    return product


def product_from_opportunity(opportunity_id: int) -> dict[str, Any]:
    opp = get_opportunity(opportunity_id)
    if not opp:
        raise KeyError("opportunity not found")
    # Product generation is a consequential action, so it stays behind the deterministic
    # current-snapshot commercial validation gate even though discovery analysis is broader.
    require_current_validation(opportunity_id)
    if opp.get("overall_score") is None or int(opp.get("analysis_stale") or 0):
        raise ValueError("opportunity must be analyzed first")
    if int(opp.get("overall_score") or 0) < settings.min_score_for_product:
        raise ValueError(f"overall score below MIN_SCORE_FOR_PRODUCT={settings.min_score_for_product}")
    if int(opp.get("copyright_risk") or 0) >= settings.high_risk_threshold:
        raise ValueError("high copyright/compliance risk; product generation blocked")
    evidence = get_evidence_for_opportunity(opportunity_id, 50)
    analysis = json.loads(opp.get("analysis_json") or "{}")
    product = _validate_product_draft(generate_product(opp, evidence, analysis, chat=provider_chat_json))
    sku = f"DG-{opportunity_id:06d}-{secrets.token_hex(2).upper()}"
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO products(opportunity_id,sku,name,category,audience,price_suggestion,price_currency,title,description,keywords_json,deliverables_json,faq_json,compliance_notes,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id,
                sku,
                product["name"],
                product.get("category"),
                product.get("audience"),
                float(product["price_suggestion"]),
                settings.default_product_currency.upper(),
                product.get("title"),
                product.get("description"),
                dumps(product.get("keywords") or []),
                dumps(product.get("deliverables") or []),
                dumps(product.get("faq") or []),
                product.get("compliance_notes"),
                "DRAFT",
                now,
                now,
            ),
        )
        pid = int(cur.lastrowid)
        c.execute("UPDATE opportunities SET status='PRODUCT_DRAFTED',updated_at=? WHERE id=?", (now, opportunity_id))
    return {"product_id": pid, "sku": sku, "draft": product}
