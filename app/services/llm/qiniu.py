from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ...security import redact_error

from ...config import settings
from ...db import conn, now_iso
from .budget import BudgetExceededError, release_ai_budget, reserve_ai_budget


class QiniuError(RuntimeError):
    pass


# Qiniu public model-plaza list price, CNY per 1M tokens. Explicit .env prices always win.
# Keep this deliberately small: unknown models stay unpriced instead of receiving a fake estimate.
_PUBLIC_PRICE_PER_MILLION: dict[str, tuple[float, float]] = {
    "qwen/qwen3.5-plus": (0.8, 4.8),
    "qwen3.5-plus": (0.8, 4.8),
}

# All worker threads in one API process share one scheduler. A condition variable is used
# rather than a plain lock so an RPM 429 can extend the cooldown while other workers are
# already waiting for their next request slot.
_RATE_CONDITION = threading.Condition()
_LAST_REQUEST_STARTED_AT = 0.0
_RATE_LIMIT_BLOCKED_UNTIL = 0.0


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
            "SELECT coalesce(sum(estimated_cost_cny),0) cost FROM ai_runs WHERE substr(created_at,1,10)=?",
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


def _request_budget_reserve(messages: list[dict[str, Any]], max_new_tokens: int | None) -> float:
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    prompt_upper = max(256, int(len(serialized) * 1.25) + 256)
    output_limit = max(1, int(max_new_tokens or settings.agent_max_new_tokens or 800))
    retry_limit = max(output_limit, _expanded_json_retry_tokens(output_limit))
    estimate = _estimate_cost(prompt_upper * 2, output_limit + retry_limit)
    return estimate if estimate > 0 else max(0.000001, float(settings.ai_unknown_call_reserve_cny or 0.0))


def _wait_for_request_slot() -> None:
    global _LAST_REQUEST_STARTED_AT
    interval = max(0.0, float(settings.qiniu_min_request_interval_seconds or 0.0))
    with _RATE_CONDITION:
        while True:
            now = time.monotonic()
            earliest = max(_LAST_REQUEST_STARTED_AT + interval, _RATE_LIMIT_BLOCKED_UNTIL)
            wait_seconds = max(0.0, earliest - now)
            if wait_seconds <= 0:
                _LAST_REQUEST_STARTED_AT = now
                return
            _RATE_CONDITION.wait(timeout=wait_seconds)


def _set_global_rate_limit_cooldown(seconds: float) -> None:
    global _RATE_LIMIT_BLOCKED_UNTIL
    delay = max(0.0, float(seconds or 0.0))
    if delay <= 0:
        return
    with _RATE_CONDITION:
        _RATE_LIMIT_BLOCKED_UNTIL = max(_RATE_LIMIT_BLOCKED_UNTIL, time.monotonic() + delay)
        _RATE_CONDITION.notify_all()


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    try:
        raw = response.headers.get("Retry-After")
        if raw not in (None, ""):
            parsed = float(raw)
            if parsed > 0:
                return parsed
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
        delay = max(
            _retry_after_seconds(response, attempt),
            max(0.0, float(settings.qiniu_rate_limit_global_cooldown_seconds or 0.0)),
        )
        _set_global_rate_limit_cooldown(delay)
    assert last_response is not None
    return last_response


def _decode_completion_response(response: httpx.Response, usage_parts=None) -> tuple[dict[str, Any], str, str]:
    if response.status_code != 200:
        raise QiniuError(f"Qiniu HTTP {response.status_code}: {redact_error(response.text)}")
    try:
        data = response.json()
    except Exception as exc:
        raise QiniuError(f"Qiniu invalid json response: {redact_error(response.text[:800])}") from exc
    if usage_parts is not None:
        usage_parts.append(_usage_numbers(data))
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except Exception as exc:
        raise QiniuError(f"Qiniu response format invalid: {str(data)[:800]}") from exc
    finish_reason = str(choice.get("finish_reason") or "").strip().lower()
    return data, str(content), finish_reason


def _usage_numbers(data: dict[str, Any]) -> tuple[int, int, int]:
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    return prompt_tokens, completion_tokens, total_tokens


def _looks_like_truncated_json(content: str, finish_reason: str) -> bool:
    if finish_reason in {"length", "max_tokens", "max_token", "token_limit"}:
        return True
    raw = str(content or "").strip()
    if raw.startswith("```json"):
        raw = raw[7:].lstrip()
    elif raw.startswith("```"):
        raw = raw[3:].lstrip()
    # Qiniu/OpenAI-compatible models may return finish_reason inconsistently. A JSON object
    # that clearly starts but never closes is safe to classify as truncation for one retry.
    return raw.startswith("{") and not raw.rstrip().endswith("}")


def _expanded_json_retry_tokens(original: int) -> int:
    configured = max(1, int(settings.qiniu_json_retry_max_tokens or 1600))
    return min(configured, max(int(original) * 2, 1200))


def chat_json_with_meta(**kwargs) -> tuple[dict[str, Any], dict[str, Any]]:
    if not settings.is_qiniu_configured:
        raise QiniuError("Qiniu is not configured. Set QINIU_API_TOKEN/QINIU_BASE_URL/QINIU_MODEL.")
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
    max_new_tokens = int(kwargs.get("max_new_tokens") or settings.agent_max_new_tokens)
    body["max_tokens"] = max_new_tokens

    operation = str(kwargs.get("operation") or "chat_json")
    entity_type = str(kwargs.get("entity_type") or "system")
    entity_id = kwargs.get("entity_id")
    created_at = now_iso()
    reservation_id: int | None = None
    usage_parts: list[tuple[int, int, int]] = []
    request_dispatched = False
    try:
        try:
            reservation_id = reserve_ai_budget(
                "Qiniu",
                _request_budget_reserve(messages, int(max_new_tokens) if max_new_tokens else None),
            )
        except BudgetExceededError as exc:
            raise QiniuError(redact_error(exc)) from exc
        request_dispatched = True
        response = _post_with_rate_limit_retry(url, headers, body)
        data, content, finish_reason = _decode_completion_response(response, usage_parts)
        json_length_retry = False
        try:
            result = _extract_json(content)
        except QiniuError:
            original_limit = int(max_new_tokens or body.get("max_tokens") or 0)
            if not original_limit or not _looks_like_truncated_json(content, finish_reason):
                raise
            retry_body = dict(body)
            retry_body["max_tokens"] = _expanded_json_retry_tokens(original_limit)
            retry_response = _post_with_rate_limit_retry(url, headers, retry_body)
            retry_data, retry_content, retry_finish_reason = _decode_completion_response(retry_response, usage_parts)
            try:
                result = _extract_json(retry_content)
            except QiniuError as exc:
                raise QiniuError(
                    f"Qiniu JSON remained invalid after truncation retry "
                    f"({original_limit}->{retry_body['max_tokens']} tokens, finish_reason={retry_finish_reason or 'unknown'}): {exc}"
                ) from exc
            data = retry_data
            finish_reason = retry_finish_reason
            json_length_retry = True

        prompt_tokens = sum(part[0] for part in usage_parts)
        completion_tokens = sum(part[1] for part in usage_parts)
        total_tokens = sum(part[2] for part in usage_parts)
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
            if not request_dispatched or (total_tokens > 0 and _resolved_prices()[2] != "unknown"):
                release_ai_budget(reservation_id, c)
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
                "json_length_retry": json_length_retry,
                "finish_reason": finish_reason or None,
            },
        }
        return result, meta
    except Exception as exc:
        prompt_tokens = sum(part[0] for part in usage_parts)
        completion_tokens = sum(part[1] for part in usage_parts)
        total_tokens = sum(part[2] for part in usage_parts)
        cost = _estimate_cost(prompt_tokens, completion_tokens)
        with conn() as c:
            c.execute(
                """INSERT INTO ai_runs(operation,entity_type,entity_id,provider,model,status,error,created_at,
                                       prompt_tokens,completion_tokens,total_tokens,estimated_cost_cny)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation,
                    entity_type,
                    entity_id,
                    "Qiniu",
                    settings.qiniu_model or "UNCONFIGURED",
                    "FAILED",
                    redact_error(exc),
                    created_at,
                    prompt_tokens, completion_tokens, total_tokens, cost,
                ),
            )
            if not request_dispatched or (total_tokens > 0 and _resolved_prices()[2] != "unknown"):
                release_ai_budget(reservation_id, c)
        if isinstance(exc, QiniuError):
            raise
        raise QiniuError(f"Qiniu connection failed: {redact_error(exc)}") from exc


def chat_json(**kwargs) -> dict[str, Any]:
    result, _ = chat_json_with_meta(**kwargs)
    return result
