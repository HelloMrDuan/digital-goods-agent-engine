from __future__ import annotations

from app import main


def test_manual_batch_can_force_retry_backoff_sources(monkeypatch):
    calls = []

    def fake_collect_source(source_id, limit=None, force=False):
        calls.append((source_id, force))
        return {"source_id": source_id, "status": "EMPTY", "fetched": 0}

    monkeypatch.setattr(main, "collect_source", fake_collect_source)
    monkeypatch.setattr(main, "build_from_unlinked_evidence", lambda: {"ok": True})

    result = main._collect_source_batch(
        [{"id": 7, "name": "reddit-test", "platform": "reddit", "priority": "P0"}],
        "core_demand",
        force=True,
    )

    assert calls == [(7, True)]
    assert result["processed"] == 1
    assert result["results"][0]["status"] == "EMPTY"
