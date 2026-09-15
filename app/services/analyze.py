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


def analyze_one(opportunity_id: int) -> dict[str, Any]:
    """Run the V1.2 evidence-first multi-Agent workflow.

    Commercial validation is still calculated and included in the card, but it no longer
    blocks the analysis itself. This lets Signal/Pain/Market agents inspect a real evidence
    candidate and safely return WATCH/DROP instead of silently discarding it before AI.
    Product generation remains protected by the deterministic validation gate below.
    """
    result = run_opportunity_agent_workflow(opportunity_id)
    card = result.get("card") or {}
    return {
        "id": opportunity_id,
        "status": "ANALYZED" if card.get("decision") != "DROP" else "REJECTED_RISK",
        "analysis": card,
        "card_id": result.get("card_id"),
    }


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
    product = generate_product(opp, evidence, analysis, chat=provider_chat_json)
    sku = f"DG-{opportunity_id:06d}-{secrets.token_hex(2).upper()}"
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO products(opportunity_id,sku,name,category,audience,price_suggestion,title,description,keywords_json,deliverables_json,faq_json,compliance_notes,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id,
                sku,
                product.get("name") or opp.get("suggested_product") or opp.get("title"),
                product.get("category"),
                product.get("audience"),
                float(product.get("price_suggestion") or 0),
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
