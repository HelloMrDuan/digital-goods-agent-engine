from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import settings
from ..db import conn, dumps, now_iso


class SenseNovaError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise SenseNovaError(f"SenseNova did not return JSON: {text[:400]}")
    try:
        value = json.loads(m.group(0))
    except Exception as exc:
        raise SenseNovaError(f"SenseNova returned invalid JSON: {text[:400]}") from exc
    if not isinstance(value, dict):
        raise SenseNovaError("SenseNova JSON root must be an object")
    return value


def _daily_spend() -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    with conn() as c:
        row = c.execute(
            "SELECT coalesce(sum(estimated_cost_cny),0) cost FROM ai_runs WHERE status='SUCCESS' AND substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
    return float(row["cost"] or 0)


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        prompt_tokens / 1_000_000 * settings.sensenova_input_price_per_million
        + completion_tokens / 1_000_000 * settings.sensenova_output_price_per_million,
        6,
    )


def chat_json(*, operation: str, entity_type: str, entity_id: int | None, system: str, payload: dict[str, Any], max_new_tokens: int = 1800) -> dict[str, Any]:
    if not settings.is_sensenova_configured:
        raise SenseNovaError("SenseNova is not configured. Set SENSENOVA_API_TOKEN and SENSENOVA_MODEL. No mock fallback is allowed.")
    if _daily_spend() >= settings.daily_ai_budget_cny:
        raise SenseNovaError(f"Daily AI budget reached: ¥{_daily_spend():.4f} / ¥{settings.daily_ai_budget_cny:.2f}")

    request_body = {
        "model": settings.sensenova_model,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": system}]},
            {"role": "user", "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]},
        ],
        "max_new_tokens": max_new_tokens,
        "repetition_penalty": 1.0,
        "stream": False,
        "temperature": 0.2,
        "top_p": 0.7,
    }
    headers = {"Authorization": f"Bearer {settings.sensenova_api_token}", "Content-Type": "application/json"}
    created_at = now_iso()
    try:
        with httpx.Client(timeout=90.0, headers=headers) as client:
            resp = client.post(settings.sensenova_base_url, json=request_body)
            resp.raise_for_status()
            body = resp.json()
        data = body.get("data") or body
        choices = data.get("choices") or []
        if not choices:
            raise SenseNovaError(f"Unexpected SenseNova response: {str(body)[:500]}")
        message = choices[0].get("message", "")
        if isinstance(message, dict):
            content = message.get("content", "")
            if isinstance(content, list):
                message = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in content)
            else:
                message = str(content)
        result = _extract_json(str(message))
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cost = _estimate_cost(prompt_tokens, completion_tokens)
        with conn() as c:
            c.execute(
                """INSERT INTO ai_runs(operation,entity_type,entity_id,provider,model,status,prompt_tokens,completion_tokens,total_tokens,estimated_cost_cny,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (operation, entity_type, entity_id, "SenseNova", settings.sensenova_model, "SUCCESS", prompt_tokens, completion_tokens, total_tokens, cost, created_at),
            )
        result["_usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens, "estimated_cost_cny": cost}
        return result
    except Exception as exc:
        with conn() as c:
            c.execute(
                """INSERT INTO ai_runs(operation,entity_type,entity_id,provider,model,status,error,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (operation, entity_type, entity_id, "SenseNova", settings.sensenova_model or "UNCONFIGURED", "FAILED", str(exc), created_at),
            )
        if isinstance(exc, SenseNovaError):
            raise
        raise SenseNovaError(str(exc)) from exc


def real_metrics(evidence: list[dict[str, Any]], opportunity: dict[str, Any] | None = None) -> dict[str, Any]:
    platform_counts = Counter(str(x.get("platform") or "unknown") for x in evidence)
    engagement_sums: Counter[str] = Counter()
    published = []
    for x in evidence:
        for k, v in (x.get("engagement") or {}).items():
            if isinstance(v, (int, float)):
                engagement_sums[k] += v
        if x.get("published_at"):
            published.append(str(x["published_at"]))
    return {
        "evidence_count": len(evidence),
        "platform_counts": dict(platform_counts),
        "engagement_sums": dict(engagement_sums),
        "published_at_min": min(published) if published else None,
        "published_at_max": max(published) if published else None,
        "trend": {
            "current_7d": (opportunity or {}).get("current_7d"),
            "previous_7d": (opportunity or {}).get("previous_7d"),
            "growth_7d_pct": (opportunity or {}).get("growth_7d_pct"),
            "trend_score": (opportunity or {}).get("trend_score"),
            "platform_count": (opportunity or {}).get("platform_count"),
            "source_count": (opportunity or {}).get("source_count"),
            "trend_updated_at": (opportunity or {}).get("trend_updated_at"),
            "note": "derived from real evidence timestamps; growth is null when prior week count is zero",
        },
    }


ANALYZE_SYSTEM = """你是数字商品商机分析师。你只能依据输入中的真实 evidence 和 real_metrics 做判断。
严禁编造不存在的搜索量、销量、用户数量、趋势百分比、竞品数量或平台数据。
如果证据不足，请明确输出 uncertainty，并降低相关评分；real_metrics.trend 中的计数和增长率来自真实 evidence 时间戳；growth_7d_pct=null 时表示上一个7天窗口为0，不得自行编造增长百分比。
目标是发现合法、可原创生产、低售后、可自动交付的数字商品机会，不局限技术类，可包含办公、求职、内容创作、小商家经营、学习效率、AI工作流等。
若证据指向盗版课程、破解软件、未授权电子书/资料包/账号/会员内容，copyright_risk 必须 >=90。
所有评分都是“基于当前证据的 AI 判断”，不是事实数据。只输出一个 JSON 对象，不要 Markdown。"""


def analyze_opportunity(opportunity: dict[str, Any], evidence: list[dict[str, Any]], *, chat=chat_json) -> dict[str, Any]:
    snippets = []
    for e in evidence[:40]:
        snippets.append({
            "evidence_id": e.get("id"),
            "platform": e.get("platform"),
            "source_matches": e.get("source_matches") or [],
            "source_url": e.get("source_url"),
            "source_item_id": e.get("source_item_id"),
            "title": e.get("title"),
            "content": (e.get("content") or "")[:1200],
            "published_at": e.get("published_at"),
            "collected_at": e.get("collected_at"),
            "engagement": e.get("engagement") or {},
        })
    metrics = real_metrics(evidence, opportunity)
    payload = {
        "candidate": {"id": opportunity["id"], "query_or_need": opportunity.get("normalized_need") or opportunity.get("title")},
        "real_metrics": metrics,
        "evidence": snippets,
        "output_schema": {
            "normalized_need": "string",
            "category": "string",
            "demand_score": "integer 0-100; AI judgement grounded only in evidence",
            "purchase_intent": "integer 0-100; AI judgement grounded only in explicit intent language",
            "competition": "integer 0-100; use 50 when competition evidence is unavailable and explain uncertainty",
            "production_cost": "integer 0-100",
            "after_sales_cost": "integer 0-100",
            "copyright_risk": "integer 0-100",
            "automation_fit": "integer 0-100",
            "suggested_product": "string",
            "reason": "string with evidence-based reasoning; do not invent metrics",
            "uncertainty": "string",
            "evidence_ids_used": ["integer IDs from input only"],
            "compliance": "string",
        },
    }
    result = chat(operation="opportunity_analysis", entity_type="opportunity", entity_id=int(opportunity["id"]), system=ANALYZE_SYSTEM, payload=payload, max_new_tokens=1900)
    return result


PRODUCT_SYSTEM = """你是数字商品产品经理。只根据输入的真实需求证据和已完成的商机分析设计原创数字商品草稿。
不得声称不存在的销量、效果、认证、用户数量或市场数据。价格只作为 AI 建议价格，不得称为市场均价。
不得复制或打包转售他人受版权保护内容。优先原创模板、表格、工作流、脚本、指南、检查表、知识索引和辅助工具。
只输出一个 JSON 对象，不要 Markdown。"""


def generate_product(opportunity: dict[str, Any], evidence: list[dict[str, Any]], analysis: dict[str, Any], *, chat=chat_json) -> dict[str, Any]:
    payload = {
        "opportunity": {k: opportunity.get(k) for k in ("id", "title", "normalized_need", "category", "overall_score", "suggested_product")},
        "analysis": {k: analysis.get(k) for k in ("normalized_need", "category", "reason", "uncertainty", "compliance", "evidence_ids_used")},
        "evidence": [
            {"id": e.get("id"), "platform": e.get("platform"), "source_url": e.get("source_url"), "title": e.get("title"), "content": (e.get("content") or "")[:700]}
            for e in evidence[:20]
        ],
        "output_schema": {
            "name": "string",
            "category": "string",
            "audience": "string",
            "price_suggestion": "number; AI suggested price only",
            "title": "string <=60 Chinese chars",
            "description": "string",
            "keywords": ["string"],
            "deliverables": ["original deliverable description"],
            "faq": [{"q": "string", "a": "string"}],
            "compliance_notes": "string",
        },
    }
    return chat(operation="product_draft", entity_type="opportunity", entity_id=int(opportunity["id"]), system=PRODUCT_SYSTEM, payload=payload, max_new_tokens=1800)
