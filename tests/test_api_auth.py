from __future__ import annotations

from app.config import settings
from app.main import _write_auth_error


def test_remote_mutation_is_blocked_when_token_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "")
    result = _write_auth_error("POST", "/api/opportunities/rebuild", "10.0.0.8", None, None)
    assert result is not None
    assert result[0] == 403


def test_loopback_mutation_stays_available_without_token(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "")
    assert _write_auth_error("POST", "/api/opportunities/rebuild", "127.0.0.1", None, None) is None


def test_configured_token_is_required_even_for_loopback(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "secret-token")
    missing = _write_auth_error("POST", "/api/products/1/approve", "127.0.0.1", None, None)
    assert missing is not None and missing[0] == 401
    assert _write_auth_error(
        "POST", "/api/products/1/approve", "10.0.0.8", "Bearer secret-token", None
    ) is None
    assert _write_auth_error(
        "PATCH", "/api/source-assets/1", "10.0.0.8", None, "secret-token"
    ) is None


def test_read_only_api_is_not_token_gated(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "secret-token")
    assert _write_auth_error("GET", "/api/opportunities", "10.0.0.8", None, None) is None
