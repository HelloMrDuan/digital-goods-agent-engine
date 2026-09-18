from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.db import conn, init_db
from app.services.llm.budget import BudgetExceededError, budget_snapshot, reserve_ai_budget, release_ai_budget
from app.services.llm import qiniu


class _Response:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }


class _RateLimitedResponse:
    status_code = 429
    text = '{"error":{"code":"rate_limit_rpm_exceeded"}}'
    headers = {"Retry-After": "0"}

    def json(self):
        return {"error": {"code": "rate_limit_rpm_exceeded"}}


class _TruncatedResponse:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {
            "choices": [{"message": {"content": '{"recommended_products":[{"name":"starter"}'}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 800, "total_tokens": 1500},
        }


class _ExpandedResponse:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {
            "choices": [{"message": {"content": '{"recommended_products":[{"name":"starter"}]}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 300, "total_tokens": 1000},
        }


class _Client:
    last_json = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, headers=None, json=None):
        _Client.last_json = json
        return _Response()


class _RetryClient(_Client):
    calls = 0

    def post(self, url, headers=None, json=None):
        type(self).calls += 1
        _Client.last_json = json
        if type(self).calls == 1:
            return _RateLimitedResponse()
        return _Response()


class _TruncationRetryClient(_Client):
    calls = 0
    bodies = []

    def post(self, url, headers=None, json=None):
        type(self).calls += 1
        type(self).bodies.append(dict(json or {}))
        _Client.last_json = json
        if type(self).calls == 1:
            return _TruncatedResponse()
        return _ExpandedResponse()


@pytest.fixture()
def qiniu_env(tmp_path: Path):
    old = {
        "db_path": settings.db_path,
        "qiniu_api_token": settings.qiniu_api_token,
        "qiniu_model": settings.qiniu_model,
        "qiniu_input_price_per_million": settings.qiniu_input_price_per_million,
        "qiniu_output_price_per_million": settings.qiniu_output_price_per_million,
        "qiniu_enable_thinking": settings.qiniu_enable_thinking,
        "qiniu_json_mode": settings.qiniu_json_mode,
        "qiniu_min_request_interval_seconds": settings.qiniu_min_request_interval_seconds,
        "qiniu_rate_limit_retries": settings.qiniu_rate_limit_retries,
        "qiniu_rate_limit_backoff_seconds": settings.qiniu_rate_limit_backoff_seconds,
        "qiniu_rate_limit_global_cooldown_seconds": settings.qiniu_rate_limit_global_cooldown_seconds,
        "qiniu_json_retry_max_tokens": settings.qiniu_json_retry_max_tokens,
        "daily_ai_budget_cny": settings.daily_ai_budget_cny,
    }
    settings.db_path = str(tmp_path / "qiniu.db")
    settings.qiniu_api_token = "test-token"
    settings.qiniu_model = "qwen/qwen3.5-plus"
    settings.qiniu_input_price_per_million = 0
    settings.qiniu_output_price_per_million = 0
    settings.qiniu_enable_thinking = False
    settings.qiniu_json_mode = True
    settings.qiniu_min_request_interval_seconds = 0
    settings.qiniu_rate_limit_retries = 2
    settings.qiniu_rate_limit_backoff_seconds = 0
    settings.qiniu_rate_limit_global_cooldown_seconds = 0
    settings.qiniu_json_retry_max_tokens = 1600
    settings.daily_ai_budget_cny = 5
    qiniu._LAST_REQUEST_STARTED_AT = 0.0
    qiniu._RATE_LIMIT_BLOCKED_UNTIL = 0.0
    init_db()
    yield
    qiniu._LAST_REQUEST_STARTED_AT = 0.0
    qiniu._RATE_LIMIT_BLOCKED_UNTIL = 0.0
    for key, value in old.items():
        setattr(settings, key, value)


def test_qiniu_agent_call_disables_thinking_uses_json_mode_and_public_price(monkeypatch, qiniu_env):
    monkeypatch.setattr(qiniu.httpx, "Client", _Client)

    result, meta = qiniu.chat_json_with_meta(
        operation="test",
        system="Return JSON",
        payload={"hello": "world"},
        max_new_tokens=800,
    )

    assert result == {"ok": True}
    assert _Client.last_json["enable_thinking"] is False
    assert _Client.last_json["response_format"] == {"type": "json_object"}
    assert _Client.last_json["max_tokens"] == 800
    assert meta["usage"]["pricing_source"] == "qiniu_public_list_price"
    assert meta["usage"]["estimated_cost_cny"] == pytest.approx(0.0032)
    assert meta["usage"]["json_length_retry"] is False


def test_qiniu_retries_rpm_429_before_failing_over(monkeypatch, qiniu_env):
    _RetryClient.calls = 0
    monkeypatch.setattr(qiniu.httpx, "Client", _RetryClient)

    result, meta = qiniu.chat_json_with_meta(
        operation="test-rate-limit",
        system="Return JSON",
        payload={"hello": "world"},
        max_new_tokens=800,
    )

    assert result == {"ok": True}
    assert _RetryClient.calls == 2
    assert meta["provider"] == "Qiniu"


def test_qiniu_429_sets_process_wide_cooldown(monkeypatch, qiniu_env):
    clock = {"now": 100.0}
    monkeypatch.setattr(qiniu.time, "monotonic", lambda: clock["now"])
    settings.qiniu_rate_limit_global_cooldown_seconds = 60

    qiniu._set_global_rate_limit_cooldown(60)

    assert qiniu._RATE_LIMIT_BLOCKED_UNTIL == pytest.approx(160.0)


def test_qiniu_retry_after_zero_falls_back_to_configured_backoff(qiniu_env):
    settings.qiniu_rate_limit_backoff_seconds = 15
    assert qiniu._retry_after_seconds(_RateLimitedResponse(), 0) == pytest.approx(15.0)
    assert qiniu._retry_after_seconds(_RateLimitedResponse(), 1) == pytest.approx(30.0)


def test_qiniu_retries_clearly_truncated_json_with_larger_token_cap(monkeypatch, qiniu_env):
    _TruncationRetryClient.calls = 0
    _TruncationRetryClient.bodies = []
    monkeypatch.setattr(qiniu.httpx, "Client", _TruncationRetryClient)

    result, meta = qiniu.chat_json_with_meta(
        operation="opportunity_agent_product",
        system="Return JSON",
        payload={"hello": "world"},
        max_new_tokens=800,
    )

    assert result == {"recommended_products": [{"name": "starter"}]}
    assert _TruncationRetryClient.calls == 2
    assert _TruncationRetryClient.bodies[0]["max_tokens"] == 800
    assert _TruncationRetryClient.bodies[1]["max_tokens"] == 1600
    assert meta["usage"]["json_length_retry"] is True
    assert meta["usage"]["prompt_tokens"] == 1400
    assert meta["usage"]["completion_tokens"] == 1100
    assert meta["usage"]["total_tokens"] == 2500



def test_budget_reservation_is_atomic_and_prevents_oversubscription(qiniu_env):
    settings.daily_ai_budget_cny = 5.0
    first = reserve_ai_budget("Qiniu", 3.0)
    snapshot = budget_snapshot()
    assert snapshot["reserved_cny"] == pytest.approx(3.0)

    with pytest.raises(BudgetExceededError, match="would be exceeded"):
        reserve_ai_budget("Qiniu", 3.0)

    release_ai_budget(first)
    second = reserve_ai_budget("Qiniu", 3.0)
    release_ai_budget(second)


def test_qiniu_success_settles_budget_reservation(monkeypatch, qiniu_env):
    monkeypatch.setattr(qiniu.httpx, "Client", _Client)
    qiniu.chat_json_with_meta(
        operation="budget-settlement",
        system="Return JSON",
        payload={"hello": "world"},
        max_new_tokens=800,
    )
    with conn() as c:
        reservations = c.execute("SELECT COUNT(*) c FROM ai_budget_reservations").fetchone()
        success = c.execute(
            "SELECT COUNT(*) c FROM ai_runs WHERE operation='budget-settlement' AND status='SUCCESS'"
        ).fetchone()
    assert int(reservations["c"]) == 0
    assert int(success["c"]) == 1
