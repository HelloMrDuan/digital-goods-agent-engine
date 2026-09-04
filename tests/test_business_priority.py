from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.config import settings
from app.db import conn, init_db, loads
from app.services.sources import bootstrap_sources, create_source, list_sources


@pytest.fixture(autouse=True)
def isolated_db():
    old = settings.db_path
    with tempfile.TemporaryDirectory() as td:
        settings.db_path = str(Path(td) / "test.db")
        init_db()
        yield
    settings.db_path = old


def test_recommended_strategy_prioritizes_real_demand_and_does_not_seed_v2ex():
    result = bootstrap_sources()
    assert result["strategy_version"] == "business-priority-v2-intent-gate"
    rows = list_sources()
    assert rows
    assert not any(x["platform"] == "v2ex" for x in rows)
    p0 = [x for x in rows if x["priority"] == "P0"]
    assert {x["platform"] for x in p0} == {"xiaohongshu", "reddit"}
    assert all(x["role"] == "demand" and x["business_group"] == "core_demand" for x in p0)
    # Login-state channels must not pretend to be ready/enabled before the user configures them.
    assert all(not x["enabled"] for x in p0)
    assert all(x["role"] == "supply" for x in rows if x["priority"] == "P2")


def test_bootstrap_disables_legacy_v2ex_defaults_and_marks_them_auxiliary():
    sid = create_source({
        "name": "V2EX 程序员", "source_type": "v2ex", "platform": "v2ex",
        "query": "q", "enabled": True, "config": {"role": "demand"},
    })
    result = bootstrap_sources()
    assert result["disabled_legacy_v2ex"] == 1
    with conn() as c:
        row = c.execute("SELECT enabled,config_json FROM sources WHERE id=?", (sid,)).fetchone()
    cfg = loads(row["config_json"], {})
    assert row["enabled"] == 0
    assert cfg["priority"] == "P3"
    assert cfg["business_group"] == "auxiliary"
    assert cfg["recommended"] is False
