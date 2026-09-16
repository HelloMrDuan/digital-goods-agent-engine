from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ...config import settings
from ...db import conn, now_iso


class QiniuError(RuntimeError):
    pass


# Qiniu public model-plaza list price, CNY per 1M tokens. Explicit .env prices always win.
# Keep this deliberately small: unknown models stay unpriced instead of receiving a fake estimate.
_PUBLIC_PRICE_PER_MILLION: dict[str, tuple[float, float]] = {
    "qwen/qwen3.5-plus": (0.8, 4.8),
    "qwen3.5-plus": (0.8, 4.8),
}

# All worker threads in one API process share a single request-start pacer. This preserves
# opportunity-level concurrency while preventing a burst of 6-agent workflows from exceeding
# Qiniu account RPM quotas.
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_STARTED_AT = 0.0


def _resolved_prices() -> tuple[float, float, str]:
    configured_in = float(settings.qiniu_input_price_per_million or 0)
    configured_out = float(settings.qiniu_output_price_per_million or 0)
    if configured_in > 0 and configured_out > 0:
        return configured_in, configured_out, "configured"
    model = str(settings.qiniu_model or "").strip().lower()
    prices = _PUBLIC_PRICE_PER_MILLION.get(model)
    if prices:
        return prices[0], prices[1], "qiniu_public_list_price"
    return 0.0, 0.0, "unknown"


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
    input_price, output_price, _ = _resolved_prices()
    return round(
        prompt_tokens / 1_000_000 * input_price
        + completion_tokens / 1_000_000 * output_price,
        6,
    )


def _wait_for_request_slot() -> None:
    global _LAST_REQUEST_STARTED_AT
    interval = max(0.0, float(settings.qiniu_min_request_interval_seconds or 0.0))
    if interval <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        wait_seconds = max(0.0, (_LAST_REQUEST_STARTED_AT + interval) - now)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_REQUEST_STARTED_AT = time.monotonic()


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    try:
        raw = response.headers.get("Retry-After")
        if raw not in (None, ""):
            return max(0.0, float(raw))
    except Exception:
        pass
    base = max(0.0, float(settings.qiniu_rate_limit_backoff_seconds or 0.0))
    return min(60.0, base * (2**attempt))


def _post_with_rate_limit_retry(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    retries = max(0, int(settings.qiniu_rate_limit_retries or 0))
    last_response: httpx.Response | None = None
    for attempt in range(retries + 1):
        _wait_for_request_slot()
        with httpx.Client(timeout=120, trust_env=False) as client:
            response = client.post(url, headers=headers, json=body)
        last_response = response
        if response.status_code != 429:
            return response
        if attempt >= retries:
            return response
        delay = _retry_after_seconds(response, attempt)
        if delay > 0:
            time.sleep(delay)
    assert last_response is not None
    return last_response


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
        # Opportunity extraction needs compact structured output, not long hidden reasoning.
        "enable_thinking": bool(settings.qiniu_enable_thinking),
    }
    if settings.qiniu_json_mode:
        body["response_format"] = {"type": "json_object"}
    max_new_tokens = kwargs.get("max_new_tokens")
    if max_new_tokens:
        body["max_tokens"] = int(max_new_tokens)

    operation = str(kwargs.get("operation") or "chat_json")
    entity_type = str(kwargs.get("entity_type") or "system")
    entity_id = kwargs.get("entity_id")
    created_at = now_iso()
    try:
        response = _post_with_rate_limit_retry(url, headers, body)
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
        _, _, pricing_source = _resolved_prices()
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
                "pricing_source": pricing_source,
                "thinking_enabled": bool(settings.qiniu_enable_thinking),
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
