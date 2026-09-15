from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ...config import settings
from ...db import conn, now_iso


class QiniuError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise QiniuError(f"Qiniu content is not valid JSON object: {raw[:500]}")
    try:
        value = json.loads(match.group(0))
    except Exception as exc:
        raise QiniuError(f"Qiniu content is not valid JSON: {raw[:500]}") from exc
    if not isinstance(value, dict):
        raise QiniuError("Qiniu JSON root must be an object")
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
        prompt_tokens / 1_000_000 * settings.qiniu_input_price_per_million
        + completion_tokens / 1_000_000 * settings.qiniu_output_price_per_million,
        6,
    )


def chat_json_with_meta(**kwargs) -> tuple[dict[str, Any], dict[str, Any]]:
    if not settings.is_qiniu_configured:
        raise QiniuError("Qiniu is not configured. Set QINIU_API_TOKEN/QINIU_BASE_URL/QINIU_MODEL.")
    if _daily_spend() >= settings.daily_ai_budget_cny:
        raise QiniuError(f"Daily AI budget reached: ¥{_daily_spend():.4f} / ¥{settings.daily_ai_budget_cny:.2f}")

    url = settings.qiniu_base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    headers = {
        "Authorization": f"Bearer {settings.qiniu_api_token}",
        "Content-Type": "application/json",
    }
    messages = []
    if kwargs.get("system"):
        messages.append({"role": "system", "content": kwargs["system"]})
    messages.append(
        {
            "role": "user",
            "content": json.dumps(kwargs.get("payload", {}), ensure_ascii=False),
        }
    )
    body: dict[str, Any] = {
        "model": settings.qiniu_model,
        "messages": messages,
        "temperature": 0.2,
    }
    max_new_tokens = kwargs.get("max_new_tokens")
    if max_new_tokens:
        body["max_tokens"] = int(max_new_tokens)

    operation = str(kwargs.get("operation") or "chat_json")
    entity_type = str(kwargs.get("entity_type") or "system")
    entity_id = kwargs.get("entity_id")
    created_at = now_iso()
    try:
        with httpx.Client(timeout=120, trust_env=False) as client:
            response = client.post(url, headers=headers, json=body)
        if response.status_code != 200:
            raise QiniuError(f"Qiniu HTTP {response.status_code}: {response.text}")
        try:
            data = response.json()
        except Exception as exc:
            raise QiniuError(f"Qiniu invalid json response: {response.text[:800]}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise QiniuError(f"Qiniu response format invalid: {str(data)[:800]}") from exc

        result = _extract_json(str(content))
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cost = _estimate_cost(prompt_tokens, completion_tokens)
        with conn() as c:
            c.execute(
                """INSERT INTO ai_runs(operation,entity_type,entity_id,provider,model,status,prompt_tokens,completion_tokens,total_tokens,estimated_cost_cny,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation,
                    entity_type,
                    entity_id,
                    "Qiniu",
                    settings.qiniu_model,
                    "SUCCESS",
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cost,
                    created_at,
                ),
            )
        meta = {
            "provider": "Qiniu",
            "model": settings.qiniu_model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_cny": cost,
            },
        }
        return result, meta
    except Exception as exc:
        with conn() as c:
            c.execute(
                """INSERT INTO ai_runs(operation,entity_type,entity_id,provider,model,status,error,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    operation,
                    entity_type,
                    entity_id,
                    "Qiniu",
                    settings.qiniu_model or "UNCONFIGURED",
                    "FAILED",
                    str(exc),
                    created_at,
                ),
            )
        if isinstance(exc, QiniuError):
            raise
        raise QiniuError(f"Qiniu connection failed: {exc}") from exc


def chat_json(**kwargs) -> dict[str, Any]:
    result, _ = chat_json_with_meta(**kwargs)
    return result
