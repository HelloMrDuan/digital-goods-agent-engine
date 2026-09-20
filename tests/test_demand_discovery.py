from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, init_db, loads
from app.services import demand_discovery


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    old = settings.db_path
    settings.db_path = str(tmp_path / "demand-discovery.db")
    init_db()
    yield
    settings.db_path = old


def _item(item_id: str, author: str, days_ago: int, *, platform: str = "reddit") -> CollectedItem:
    return CollectedItem(
        platform=platform,
        source_type=f"agent_reach_{platform}",
        source_item_id=item_id,
        source_url=f"https://example.com/{item_id}",
        title=f"looking for a template {item_id}",
        content="I need a real template and I am looking for recommendations.",
        author=author,
        published_at=(datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
        raw={"id": item_id, "author": author},
    )


def test_prepare_items_rejects_stale_and_prefers_independent_authors():
    items = [
        _item("a-new", "alice", 1),
        _item("a-old", "alice", 2),
        _item("b", "bob", 3),
        _item("stale", "charlie", 40),
    ]
    result = demand_discovery._prepare_items(items, 3)
    assert [item.source_item_id for item in result] == ["a-new", "b", "a-old"]
    assert "stale" not in {item.source_item_id for item in result}


def test_install_sources_retires_old_narrow_p0():
    now = datetime.now(timezone.utc).isoformat()
    with conn() as c:
        c.execute(
            """INSERT INTO sources(name,source_type,platform,query,config_json,enabled,status,created_at,updated_at)
               VALUES(?,?,?,?,?,1,'READY',?,?)""",
            (
                "Reddit - productivity template demand", "agent_reach", "reddit", "old query",
                '{"role":"demand","priority":"P0","business_group":"core_demand"}', now, now,
            ),
        )
    result = demand_discovery.install_sources()
    assert result["created"] == 6
    assert result["retired_legacy"] == 1
    with conn() as c:
        old = c.execute("SELECT enabled,config_json FROM sources WHERE name='Reddit - productivity template demand'").fetchone()
        managed = c.execute("SELECT COUNT(*) c FROM sources WHERE name LIKE '% v3'").fetchone()
    cfg = loads(old["config_json"], {})
    assert old["enabled"] == 0
    assert cfg["priority"] == "P3"
    assert cfg["business_group"] == "auxiliary"
    assert managed["c"] == 4


def test_run_demand_discovery_uses_fanout_and_only_recent_items(monkeypatch):
    monkeypatch.setattr(demand_discovery, "agent_reach_doctor", lambda: {
        "opencli": {"connected": True},
        "channels": {},
    })
    counter = {"n": 0}

    def fake_collect(platform, query, limit=20, **kwargs):
        counter["n"] += 1
        prefix = "x" if platform == "xiaohongshu" else "r"
        return [
            _item(f"{prefix}-{counter['n']}-new", f"author-{counter['n']}", 1, platform=platform),
            _item(f"{prefix}-{counter['n']}-stale", f"old-{counter['n']}", 50, platform=platform),
        ]

    monkeypatch.setattr(demand_discovery, "collect_agent_reach", fake_collect)
    result = demand_discovery.run_demand_discovery()
    assert result["status"] == "SUCCESS"
    assert result["probe_count"] == 4
    assert result["successful_probes"] == 4
    assert result["retained_recent"] > 0
    assert result["direct_demand_signals"] > 0
    assert counter["n"] > 20


def test_run_demand_discovery_reports_query_progress_and_respects_cap(monkeypatch):
    monkeypatch.setattr(demand_discovery, "agent_reach_doctor", lambda: {
        "opencli": {"connected": True},
        "channels": {},
    })
    calls = []
    progress = []

    def fake_collect(platform, query, limit=20, **kwargs):
        calls.append((platform, query))
        return [_item(f"{platform}-{len(calls)}", f"author-{len(calls)}", 1, platform=platform)]

    monkeypatch.setattr(demand_discovery, "collect_agent_reach", fake_collect)
    result = demand_discovery.run_demand_discovery(
        max_queries_per_probe=2,
        progress_callback=progress.append,
    )

    assert result["total_queries"] == 8
    assert result["max_queries_per_probe"] == 2
    assert len(calls) == 8
    assert len(progress) == 8
    assert sorted(event["completed_queries"] for event in progress) == list(range(1, 9))
    assert all(event["total_queries"] == 8 for event in progress)



def test_v4_probe_matrix_targets_buildable_corroboration_topics():
    assert demand_discovery.STRATEGY_VERSION == "demand-discovery-v4-buildable-corroboration"
    queries_by_platform = {
        platform: [
            query.lower()
            for probe in demand_discovery._PROBES
            if probe["platform"] == platform
            for query in probe["queries"]
        ]
        for platform in {"xiaohongshu", "reddit"}
    }

    xhs = queries_by_platform["xiaohongshu"]
    reddit = queries_by_platform["reddit"]

    assert any("excel" in query for query in xhs)
    assert any("表格" in query for query in xhs)
    assert any("自动化" in query or "脚本" in query for query in xhs)
    assert any("spreadsheet" in query for query in reddit)
    assert any("automation" in query for query in reddit)

    # Keep queries demand-first instead of silently using marketplace supply as demand.
    assert any("求" in query for query in xhs)
    assert any("need " in query or "looking for" in query for query in reddit)


def test_all_v4_probes_fill_the_eight_query_refresh_cap():
    assert len(demand_discovery._PROBES) == 4
    assert all(len(probe["queries"]) == 8 for probe in demand_discovery._PROBES)



def test_dynamic_corroboration_uses_real_matching_topic_results(monkeypatch):
    demand_discovery.install_sources()
    now = datetime.now(timezone.utc).isoformat()
    with conn() as c:
        source = c.execute("SELECT id FROM sources WHERE name='Reddit - direct demand scan v3'").fetchone()
        source_id = int(source["id"])
        c.execute(
            """INSERT INTO evidence(
                source_id,platform,source_type,source_item_id,source_url,title,content,author,
                published_at,collected_at,engagement_json,raw_json,raw_hash,canonical_key,
                content_fingerprint,evidence_quality_score,published_time_quality,demand_signal_score,
                content_kind,intent_class,intent_confidence,intent_weight,intent_reason_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source_id,"reddit","agent_reach_reddit","seed-1","https://reddit.com/seed-1",
                "Need an inventory spreadsheet","Looking for inventory spreadsheet for my store",
                "seed-author",now,now,"{}","{}","rh1","ck1","fp1",80,"source",85,
                "post","explicit_demand",0.9,1.0,"[]",
            ),
        )
        evidence_id = int(c.execute("SELECT id FROM evidence WHERE canonical_key='ck1'").fetchone()["id"])
        c.execute(
            "INSERT INTO evidence_sources(evidence_id,source_id,first_seen_at,last_seen_at,observation_count) VALUES(?,?,?,?,1)",
            (evidence_id,source_id,now,now),
        )

    calls = []

    def fake_collect(platform, query, limit=20, **kwargs):
        calls.append((platform, query))
        return [
            CollectedItem(
                platform=platform,
                source_type=f"agent_reach_{platform}",
                source_item_id=f"{platform}-inventory",
                source_url=f"https://example.com/{platform}-inventory",
                title="Need inventory spreadsheet",
                content="Looking for an inventory spreadsheet template for a small store.",
                author=f"{platform}-buyer",
                published_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                raw={"id": f"{platform}-inventory"},
            ),
            CollectedItem(
                platform=platform,
                source_type=f"agent_reach_{platform}",
                source_item_id=f"{platform}-noise",
                source_url=f"https://example.com/{platform}-noise",
                title="Completely unrelated request",
                content="Need help with a different topic",
                author="noise",
                published_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                raw={"id": f"{platform}-noise"},
            ),
        ]

    original_item = demand_discovery._item_matches_topic
    monkeypatch.setattr(
        demand_discovery,
        "_item_matches_topic",
        lambda item, topic: "inventory" in (item.title + " " + (item.content or "")).lower(),
    )
    monkeypatch.setattr(demand_discovery, "collect_agent_reach", fake_collect)

    result = demand_discovery._run_dynamic_corroboration({"reddit", "xiaohongshu"})

    assert result["queries"] == 2
    assert {platform for platform, _ in calls} == {"reddit", "xiaohongshu"}
    assert result["topics"][0]["key"] == "inventory_spreadsheet"
    assert result["direct_demand_signals"] >= 1
