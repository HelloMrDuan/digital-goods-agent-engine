from __future__ import annotations

import json
import secrets
from typing import Any

from ..config import settings
from ..db import conn, dumps, now_iso
from .opportunities import get_evidence_for_opportunity, get_opportunity
from .sensenova import analyze_opportunity, generate_product


def _score(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return default


def analyze_one(opportunity_id: int) -> dict[str, Any]:
    opp = get_opportunity(opportunity_id)
    if not opp:
        raise KeyError("opportunity not found")
    evidence = get_evidence_for_opportunity(opportunity_id, 80)
    if len(evidence) < settings.min_evidence_for_analysis:
        raise ValueError(f"At least {settings.min_evidence_for_analysis} real evidence item(s) required; found {len(evidence)}")
    result = analyze_opportunity(opp, evidence)
    demand = _score(result.get("demand_score"))
    purchase = _score(result.get("purchase_intent"))
    competition = _score(result.get("competition"), 50)
    production = _score(result.get("production_cost"), 50)
    after_sales = _score(result.get("after_sales_cost"), 50)
    risk = _score(result.get("copyright_risk"), 50)
    automation = _score(result.get("automation_fit"), 50)
    overall = round(
        demand * 0.25 + purchase * 0.20 + (100 - competition) * 0.15 +
        (100 - production) * 0.10 + automation * 0.10 +
        (100 - after_sales) * 0.10 + (100 - risk) * 0.10
    )
    if risk >= settings.high_risk_threshold:
        overall = 0
        status = "REJECTED_RISK"
    else:
        status = "ANALYZED"
    result["overall_score"] = overall
    result["real_evidence_count"] = len(evidence)
    now = now_iso()
    with conn() as c:
        c.execute(
            """UPDATE opportunities SET normalized_need=?, category=?, status=?, demand_score=?,purchase_intent=?,competition=?,production_cost=?,after_sales_cost=?,copyright_risk=?,automation_fit=?,overall_score=?,suggested_product=?,ai_reason=?,analysis_json=?,evidence_count=?,analysis_stale=0,updated_at=? WHERE id=?""",
            (
                result.get("normalized_need") or opp.get("normalized_need") or opp.get("title"),
                result.get("category"), status, demand, purchase, competition, production, after_sales, risk, automation,
                overall, result.get("suggested_product"), result.get("reason"), dumps(result), len(evidence), now, opportunity_id,
            ),
        )
    return {"id": opportunity_id, "status": status, "analysis": result}


def product_from_opportunity(opportunity_id: int) -> dict[str, Any]:
    opp = get_opportunity(opportunity_id)
    if not opp:
        raise KeyError("opportunity not found")
    if opp.get("overall_score") is None:
        raise ValueError("opportunity must be analyzed first")
    if int(opp.get("overall_score") or 0) < settings.min_score_for_product:
        raise ValueError(f"overall score below MIN_SCORE_FOR_PRODUCT={settings.min_score_for_product}")
    if int(opp.get("copyright_risk") or 0) >= settings.high_risk_threshold:
        raise ValueError("high copyright/compliance risk; product generation blocked")
    evidence = get_evidence_for_opportunity(opportunity_id, 50)
    analysis = json.loads(opp.get("analysis_json") or "{}")
    product = generate_product(opp, evidence, analysis)
    sku = f"DG-{opportunity_id:06d}-{secrets.token_hex(2).upper()}"
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO products(opportunity_id,sku,name,category,audience,price_suggestion,title,description,keywords_json,deliverables_json,faq_json,compliance_notes,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id, sku, product.get("name") or opp.get("suggested_product") or opp.get("title"),
                product.get("category"), product.get("audience"), float(product.get("price_suggestion") or 0), product.get("title"),
                product.get("description"), dumps(product.get("keywords") or []), dumps(product.get("deliverables") or []),
                dumps(product.get("faq") or []), product.get("compliance_notes"), "DRAFT", now, now,
            ),
        )
        pid = int(cur.lastrowid)
        c.execute("UPDATE opportunities SET status='PRODUCT_DRAFTED',updated_at=? WHERE id=?", (now, opportunity_id))
    return {"product_id": pid, "sku": sku, "draft": product}
