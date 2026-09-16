from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.db import init_db
from app.services.llm import qiniu


class _Response:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }


class _RateLimitedResponse:
    status_code = 429
    text = '{"error":{"code":"rate_limit_rpm_exceeded"}}'
    headers = {"Retry-After": "0"}

    def json(self):
        return {"error": {"code": "rate_limit_rpm_exceeded"}}


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
    settings.daily_ai_budget_cny = 5
    qiniu._LAST_REQUEST_STARTED_AT = 0.0
    init_db()
    yield
    qiniu._LAST_REQUEST_STARTED_AT = 0.0
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


def test_qiniu_retries_rpm_429_before_failing_over(monkeypatch, qiniu_env):
    _RetryClient.calls = 0
    monkeypatch.setattr(qiniu.httpx, "Client", _RetryClient)
    monkeypatch.setattr(qiniu.time, "sleep", lambda _: None)

    result, meta = qiniu.chat_json_with_meta(
        operation="test-rate-limit",
        system="Return JSON",
        payload={"hello": "world"},
        max_new_tokens=800,
    )

    assert result == {"ok": True}
    assert _RetryClient.calls == 2
    assert meta["provider"] == "Qiniu"
