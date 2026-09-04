from __future__ import annotations

from pathlib import Path

import pytest

from app import main as main_module
from app.config import settings
from app.db import init_db
from app.services.sources import bootstrap_sources, list_sources


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    old = settings.db_path
    settings.db_path = str(tmp_path / "p0-activation.db")
    init_db()
    yield
    settings.db_path = old


def _connected_doctor():
    return {
        "installed": True,
        "status": "OK",
        "opencli": {"installed": True, "connected": True, "status": "CONNECTED"},
        "channels": {
            "xiaohongshu": {"effective_status": "ok", "effective_backend": "OpenCLI Browser Bridge"},
            "reddit": {"effective_status": "ok", "effective_backend": "OpenCLI Browser Bridge"},
        },
    }


def test_activate_p0_enables_recommended_sources_only_after_real_opencli_connection(monkeypatch):
    bootstrap_sources()
    assert sum(1 for x in list_sources() if x.get("priority") == "P0" and x.get("enabled")) == 0

    monkeypatch.setattr(main_module, "agent_reach_doctor", _connected_doctor)
    result = main_module._activate_p0_from_real_login()
    assert result["ready"] is True
    assert result["eligible"] == 8
    assert result["activated"] == 8
    assert sum(1 for x in list_sources() if x.get("priority") == "P0" and x.get("enabled")) == 8


def test_activate_p0_keeps_sources_disabled_when_login_bridge_is_not_connected(monkeypatch):
    bootstrap_sources()
    monkeypatch.setattr(main_module, "agent_reach_doctor", lambda: {
        "installed": True,
        "status": "OK",
        "opencli": {"installed": True, "connected": False, "status": "NOT_CONNECTED"},
        "channels": {},
    })
    result = main_module._activate_p0_from_real_login()
    assert result["ready"] is False
    assert result["activated"] == 0
    assert sum(1 for x in list_sources() if x.get("priority") == "P0" and x.get("enabled")) == 0
