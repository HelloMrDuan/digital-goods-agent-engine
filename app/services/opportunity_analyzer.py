from __future__ import annotations

from typing import Any
from ..db import conn, dumps, now_iso
from .opportunities import get_opportunity, get_evidence_for_opportunity
from .llm import chat_json


def analyze_one_opportunity(opportunity_id: int) -> dict[str, Any]:
    opp = get_opportunity(opportunity_id)
    if not opp:
        raise ValueError('opportunity not found')
    evidence = get_evidence_for_opportunity(opportunity_id)
    result = chat_json(operation='opportunity_analysis', entity_type='opportunity', entity_id=opportunity_id, system='Analyze digital product opportunities. Return JSON only.', payload={'opportunity': opp, 'evidence': evidence})
    with conn() as c:
        c.execute(
            '''UPDATE opportunities SET
            normalized_need=?, category=?, demand_score=?, purchase_intent=?, competition=?,
            production_cost=?, after_sales_cost=?, copyright_risk=?, automation_fit=?,
            overall_score=?, suggested_product=?, ai_reason=?, analysis_json=?, status='ANALYZED', updated_at=?
            WHERE id=?''',
            (
                result.get('normalized_need'), result.get('category'), result.get('demand_score'),
                result.get('purchase_intent'), result.get('competition'), result.get('production_cost'),
                result.get('after_sales_cost'), result.get('copyright_risk'), result.get('automation_fit'),
                int(result.get('demand_score',0))*10//1 if result.get('demand_score') is not None else None,
                result.get('suggested_product'), result.get('reason'), dumps(result), now_iso(), opportunity_id
            )
        )
    return {**result, 'opportunity_id': opportunity_id}
