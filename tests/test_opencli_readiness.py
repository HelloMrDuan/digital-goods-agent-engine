from __future__ import annotations

from app import main as main_module


def test_real_refresh_readiness_rejects_disconnected_opencli(monkeypatch):
    monkeypatch.setattr(main_module, "agent_reach_doctor", lambda: {
        "installed": True,
        "status": "OK",
        "opencli": {"installed": True, "connected": False, "status": "NOT_CONNECTED"},
        "channels": {
            "xiaohongshu": {"effective_status": "ok", "effective_backend": "OpenCLI Browser Bridge"},
            "reddit": {"effective_status": "ok", "effective_backend": "OpenCLI Browser Bridge"},
        },
    })
    ready, reason = main_module._real_refresh_readiness()
    assert ready is False
    assert "not connected" in reason.lower()


def test_real_refresh_readiness_accepts_connected_opencli(monkeypatch):
    monkeypatch.setattr(main_module, "agent_reach_doctor", lambda: {
        "installed": True,
        "status": "OK",
        "opencli": {"installed": True, "connected": True, "status": "CONNECTED"},
        "channels": {},
    })
    ready, reason = main_module._real_refresh_readiness()
    assert ready is True
    assert reason is None
