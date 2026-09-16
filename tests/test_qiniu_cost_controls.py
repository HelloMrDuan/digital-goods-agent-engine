from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.db import init_db
from app.services.llm import qiniu


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
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
        "daily_ai_budget_cny": settings.daily_ai_budget_cny,
    }
    settings.db_path = str(tmp_path / "qiniu.db")
    settings.qiniu_api_token = "test-token"
    settings.qiniu_model = "qwen/qwen3.5-plus"
    settings.qiniu_input_price_per_million = 0
    settings.qiniu_output_price_per_million = 0
    settings.qiniu_enable_thinking = False
    settings.qiniu_json_mode = True
    settings.daily_ai_budget_cny = 5
    init_db()
    yield
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
