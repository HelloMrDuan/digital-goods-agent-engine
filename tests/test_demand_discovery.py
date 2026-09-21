from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

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
    assert demand_discovery.STRATEGY_VERSION == "demand-discovery-v5-topic-provenance"
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



def test_corroboration_item_persists_topic_provenance():
    item = CollectedItem(
        platform="reddit",
        source_type="agent_reach_reddit",
        source_item_id="topic-1",
        source_url="https://reddit.com/topic-1",
        title="Need inventory spreadsheet",
        content="Need stock management for a shop",
        author="buyer",
        published_at=datetime.now(timezone.utc).isoformat(),
        raw={"id": "topic-1"},
    )

    tagged = demand_discovery._tag_corroboration_item(
        item,
        "inventory_spreadsheet",
        "need inventory spreadsheet",
    )

    assert tagged.raw["_corroboration_topic"] == "inventory_spreadsheet"
    assert tagged.raw["_corroboration_query"] == "need inventory spreadsheet"


def test_broad_automation_is_not_a_corroboration_topic():
    assert demand_discovery._topic_for_text(
        "Need automation script",
        "I want to automate a random workflow",
    ) is None



def test_targeted_watch_corroboration_collects_only_one_deterministic_topic(monkeypatch):
    demand_discovery.install_sources()
    monkeypatch.setattr(
        demand_discovery,
        "get_opportunity",
        lambda opportunity_id: {
            "id": opportunity_id,
            "title": "Rental billing automation",
            "normalized_need": "invoice billing automation for rental catalogs",
        },
    )
    monkeypatch.setattr(
        demand_discovery,
        "get_evidence_for_opportunity",
        lambda opportunity_id, limit=120: [
            {
                "id": 501,
                "relation_type": "demand",
                "intent_class": "explicit_demand",
                "title": "Need invoice automation",
                "content": "Need billing automation from a saved rental item catalog.",
                "author": "seed",
                "platform": "reddit",
            }
        ],
    )
    monkeypatch.setattr(
        demand_discovery,
        "agent_reach_doctor",
        lambda: {"opencli": {"connected": True}, "channels": {}},
    )

    calls = []

    def fake_collect(platform, query, limit=20, **kwargs):
        calls.append((platform, query, limit))
        return [
            CollectedItem(
                platform=platform,
                source_type=f"agent_reach_{platform}",
                source_item_id=f"{platform}-invoice",
                source_url=f"https://example.com/{platform}-invoice",
                title="Need invoice billing automation",
                content="Looking for invoicing automation for a rental business.",
                author=f"{platform}-buyer",
                published_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                raw={"id": f"{platform}-invoice"},
            ),
            CollectedItem(
                platform=platform,
                source_type=f"agent_reach_{platform}",
                source_item_id=f"{platform}-noise",
                source_url=f"https://example.com/{platform}-noise",
                title="AI agent ideas",
                content="Completely unrelated.",
                author="noise",
                published_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                raw={"id": f"{platform}-noise"},
            ),
        ]

    monkeypatch.setattr(demand_discovery, "collect_agent_reach", fake_collect)

    result = demand_discovery.refresh_opportunity_corroboration(52, per_platform_limit=5)

    assert result["topic"] == "invoice_contract_workflow"
    assert result["queries"] == 2
    assert result["direct_demand_signals"] >= 1
    assert all(item["retained_recent"] == 1 for item in result["results"])
    assert all(call[2] == 5 for call in calls)


def test_targeted_watch_corroboration_rejects_mixed_demand_topics(monkeypatch):
    monkeypatch.setattr(
        demand_discovery,
        "get_opportunity",
        lambda opportunity_id: {"id": opportunity_id, "title": "mixed", "normalized_need": "mixed"},
    )
    monkeypatch.setattr(
        demand_discovery,
        "get_evidence_for_opportunity",
        lambda opportunity_id, limit=120: [
            {
                "id": 1,
                "relation_type": "demand",
                "intent_class": "explicit_demand",
                "title": "Need PDF to Excel OCR",
                "content": "Need PDF OCR output to Excel.",
                "author": "a",
                "platform": "reddit",
            },
            {
                "id": 2,
                "relation_type": "demand",
                "intent_class": "explicit_demand",
                "title": "Need invoice billing automation",
                "content": "Need invoicing for my rental business.",
                "author": "b",
                "platform": "reddit",
            },
        ],
    )

    with pytest.raises(ValueError, match="multiple deterministic demand topics"):
        demand_discovery._opportunity_corroboration_topic(80)



def test_parse_failure_circuit_breaker_disables_broken_platform_for_run(monkeypatch):
    monkeypatch.setattr(demand_discovery, "agent_reach_doctor", lambda: {
        "opencli": {"connected": True},
        "channels": {},
    })
    calls = {"xiaohongshu": 0, "reddit": 0}
    corroboration_platforms = []

    def fake_collect(platform, query, limit=20, **kwargs):
        calls[platform] += 1
        if platform == "xiaohongshu":
            raise RuntimeError(
                "Agent-Reach real collection failed: opencli returned no parseable real items"
            )
        return [
            _item(
                f"reddit-{calls[platform]}",
                f"author-{calls[platform]}",
                1,
                platform=platform,
            )
        ]

    def fake_corroboration(platforms, progress_callback=None):
        corroboration_platforms.extend(sorted(platforms))
        return {
            "topics": [],
            "queries": 0,
            "direct_demand_signals": 0,
        }

    monkeypatch.setattr(demand_discovery, "collect_agent_reach", fake_collect)
    monkeypatch.setattr(demand_discovery, "_run_dynamic_corroboration", fake_corroboration)

    result = demand_discovery.run_demand_discovery(max_queries_per_probe=3)

    assert calls["xiaohongshu"] <= 2
    assert calls["reddit"] == 6
    assert "xiaohongshu" in result["disabled_platforms_this_run"]
    assert result["corroboration_platforms"] == ["reddit"]
    assert corroboration_platforms == ["reddit"]
    assert result["status"] == "PARTIAL"


def test_normal_empty_or_success_does_not_trip_parse_failure_circuit():
    state = {
        "lock": threading.Lock(),
        "parse_failures": {},
        "disabled": {},
    }
    demand_discovery._record_platform_query_result(state, "xiaohongshu", None)
    demand_discovery._record_platform_query_result(
        state,
        "xiaohongshu",
        "EMPTY_RESULT: no matching posts",
    )

    is_open, reason = demand_discovery._platform_circuit_open(state, "xiaohongshu")

    assert is_open is False
    assert reason is None



def test_xiaohongshu_opencli_command_uses_current_json_contract(monkeypatch):
    from app.collectors import agent_reach

    monkeypatch.setattr(agent_reach.shutil, "which", lambda name: f"C:/tools/{name}.cmd" if name == "opencli" else None)
    commands = agent_reach._commands(
        "xiaohongshu",
        "求 Excel 模板",
        12,
        "OpenCLI Browser Bridge",
    )

    assert commands == [[
        "opencli",
        "xiaohongshu",
        "search",
        "求 Excel 模板",
        "--limit",
        "12",
        "-f",
        "json",
    ]]


def test_opencli_installed_but_disconnected_is_not_channel_ready(monkeypatch):
    from app.collectors import agent_reach

    monkeypatch.setattr(agent_reach.shutil, "which", lambda name: None if name == "agent-reach" else "opencli.cmd")
    monkeypatch.setattr(
        agent_reach,
        "_opencli_doctor",
        lambda: {
            "installed": True,
            "connected": False,
            "status": "NOT_CONNECTED",
        },
    )

    status = agent_reach.doctor()

    assert status["status"] == "NOT_CONNECTED"
    assert status["channels"]["xiaohongshu"]["effective_status"] == "not_connected"
    assert status["channels"]["xiaohongshu"]["opencli_connected"] is False


def test_payload_shape_exposes_opencli_schema_drift():
    from app.collectors import agent_reach

    assert agent_reach._payload_shape([
        {"noteId": "1", "title": "hello", "author": "buyer"}
    ]) == "list[dict] keys=author,noteId,title"



def test_opencli_empty_list_is_normal_empty_not_parse_failure(monkeypatch):
    from app.collectors import agent_reach

    monkeypatch.setattr(
        agent_reach,
        "doctor",
        lambda: {
            "installed": True,
            "status": "OK",
            "channels": {
                "xiaohongshu": {
                    "effective_status": "ok",
                    "effective_backend": "OpenCLI Browser Bridge",
                    "opencli_connected": True,
                }
            },
        },
    )
    monkeypatch.setattr(
        agent_reach,
        "_commands",
        lambda platform, query, limit, active_backend: [[
            "opencli", "xiaohongshu", "search", query, "--limit", str(limit), "-f", "json"
        ]],
    )
    monkeypatch.setattr(agent_reach, "_run", lambda cmd: [])

    result = agent_reach.collect("xiaohongshu", "求 Excel 模板", limit=5)

    assert result == []


def test_nonempty_unparseable_payload_still_counts_as_parse_failure(monkeypatch):
    from app.collectors import agent_reach

    monkeypatch.setattr(
        agent_reach,
        "doctor",
        lambda: {
            "installed": True,
            "status": "OK",
            "channels": {
                "xiaohongshu": {
                    "effective_status": "ok",
                    "effective_backend": "OpenCLI Browser Bridge",
                    "opencli_connected": True,
                }
            },
        },
    )
    monkeypatch.setattr(
        agent_reach,
        "_commands",
        lambda platform, query, limit, active_backend: [[
            "opencli", "xiaohongshu", "search", query, "--limit", str(limit), "-f", "json"
        ]],
    )
    # Non-empty payload exists, but has neither title nor usable normalized fields.
    monkeypatch.setattr(agent_reach, "_run", lambda cmd: [{"foo": "bar"}])

    with pytest.raises(Exception, match="no parseable real items"):
        agent_reach.collect("xiaohongshu", "求 Excel 模板", limit=5)


def test_empty_result_does_not_trip_discovery_circuit(monkeypatch):
    demand_discovery.install_sources()
    probe = next(item for item in demand_discovery._PROBES if item["platform"] == "xiaohongshu")
    state = {
        "lock": threading.Lock(),
        "parse_failures": {},
        "disabled": {},
    }
    monkeypatch.setattr(demand_discovery, "collect_agent_reach", lambda *args, **kwargs: [])

    result = demand_discovery._collect_probe(
        probe,
        queries=["求 Excel 模板"],
        platform_circuit=state,
    )

    assert result["status"] == "EMPTY"
    assert result["query_failures"] == 0
    assert result["circuit_open"] is False
    assert state["disabled"] == {}
