from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem, CollectorError
from app.config import settings
from app.db import conn, init_db
from app.services.clustering import trend_detail
from app.services.ingest import insert_evidence
from app.services.opportunities import build_from_unlinked_evidence, list_opportunities
from app.services.sources import collect_source, create_source, get_source
import app.services.sources as sources_service


@pytest.fixture(autouse=True)
def isolated_db():
    old_db = settings.db_path
    old_threshold = settings.cluster_similarity_threshold
    old_support = settings.trend_support_similarity_threshold
    old_retry = settings.collector_retries
    with tempfile.TemporaryDirectory() as td:
        settings.db_path = str(Path(td) / "test.db")
        settings.cluster_similarity_threshold = 0.24
        settings.trend_support_similarity_threshold = 0.24
        settings.collector_retries = 0
        init_db()
        yield
    settings.db_path = old_db
    settings.cluster_similarity_threshold = old_threshold
    settings.trend_support_similarity_threshold = old_support
    settings.collector_retries = old_retry


def _source(name: str, platform: str, role: str, source_type: str = "v2ex", **cfg) -> dict:
    config = {"role": role, **cfg}
    sid = create_source({"name": name, "source_type": source_type, "platform": platform, "query": "q", "enabled": True, "config": config})
    return {"id": sid}


def _item(platform: str, item_id: str, title: str, content: str, author: str | None = "u", published: bool = True) -> CollectedItem:
    return CollectedItem(
        platform=platform,
        source_type="test",
        source_item_id=item_id,
        source_url=f"https://{platform}.example/{item_id}",
        title=title,
        content=content,
        author=author,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds") if published else None,
        engagement={"likes": 3},
        raw={"id": item_id, "title": title, "content": content},
    )


def test_mirrored_content_keeps_provenance_but_counts_once_for_trend():
    xhs = _source("xhs", "xiaohongshu", "demand", "agent_reach")
    reddit = _source("reddit", "reddit", "demand", "agent_reach")
    title = "求一个工资考勤自动计算Excel模板"
    content = "小公司需要工资考勤自动计算，最好Excel自动出工资表"
    insert_evidence(xhs, _item("xiaohongshu", "x1", title, content, "alice"))
    insert_evidence(reddit, _item("reddit", "r1", title, content, "bob"))
    build_from_unlinked_evidence()
    opp = list_opportunities()[0]
    assert opp["evidence_count"] == 2
    assert opp["platform_count"] == 2
    assert opp["effective_signal_count"] == 1
    trend = trend_detail(opp["id"])
    assert trend["evidence_total"] == 2
    assert trend["effective_signal_count"] == 1
    assert trend["current_7d"] == 1


def test_trend_evidence_is_support_only_and_does_not_inflate_demand_count():
    demand = _source("demand", "xiaohongshu", "demand", "agent_reach")
    trend = _source("trend", "google_news", "trend", "google_news")
    insert_evidence(demand, _item("xiaohongshu", "x1", "求工资考勤自动计算Excel模板", "需要自动工资考勤Excel表", "alice"))
    insert_evidence(trend, _item("google_news", "n1", "工资考勤自动计算Excel模板市场关注", "工资考勤自动计算Excel工具相关报道", "media"))
    build_from_unlinked_evidence()
    opp = list_opportunities()[0]
    assert opp["evidence_count"] == 1
    assert opp["support_evidence_count"] == 1
    assert opp["current_7d"] == 1
    with conn() as c:
        rows = c.execute("SELECT relation_type,COUNT(*) c FROM opportunity_evidence GROUP BY relation_type").fetchall()
    assert {r["relation_type"]: r["c"] for r in rows} == {"demand": 1, "support": 1}


def test_supply_never_becomes_support_or_demand():
    demand = _source("demand", "reddit", "demand", "agent_reach")
    supply = _source("supply", "github", "supply", "github")
    insert_evidence(demand, _item("reddit", "d1", "求工资考勤自动计算Excel模板", "需要工资考勤自动计算", "alice"))
    insert_evidence(supply, _item("github", "g1", "工资考勤自动计算Excel模板", "开源工资考勤自动化项目", "repo"))
    build_from_unlinked_evidence()
    opp = list_opportunities()[0]
    assert opp["evidence_count"] == 1
    assert opp["support_evidence_count"] == 0


def test_disappeared_cluster_becomes_inactive_instead_of_staying_in_rankings():
    sid = create_source({"name":"switchable","source_type":"agent_reach","platform":"xiaohongshu","query":"q","enabled":True,"config":{"role":"demand","priority":"P0","business_group":"core_demand"}})
    insert_evidence({"id": sid}, _item("xiaohongshu", "d1", "求库存管理Excel模板", "需要库存出入库自动统计Excel", "alice"))
    build_from_unlinked_evidence()
    assert len(list_opportunities()) == 1
    with conn() as c:
        c.execute("UPDATE sources SET config_json=? WHERE id=?", (json.dumps({"role":"supply"}), sid))
    build_from_unlinked_evidence()
    assert list_opportunities() == []
    with conn() as c:
        assert c.execute("SELECT cluster_active FROM opportunities").fetchone()["cluster_active"] == 0


def test_source_minimum_interval_skips_duplicate_network_call(monkeypatch):
    sid = create_source({"name":"paced","source_type":"v2ex","platform":"v2ex","query":"q","enabled":True,"config":{"role":"demand","min_interval_minutes":60}})
    calls = {"n": 0}
    def fake_dispatch(source, config, limit):
        calls["n"] += 1
        return [_item("v2ex", f"i{calls['n']}", "求库存Excel模板", "需要库存自动统计Excel", "alice")]
    monkeypatch.setattr(sources_service, "_dispatch", fake_dispatch)
    first = collect_source(sid)
    second = collect_source(sid)
    assert first["status"] == "SUCCESS"
    assert second["status"] == "SKIPPED"
    assert calls["n"] == 1
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM collection_runs").fetchone()["c"] == 1


def test_failed_source_enters_backoff_and_does_not_hammer_endpoint(monkeypatch):
    sid = create_source({"name":"broken","source_type":"v2ex","platform":"v2ex","query":"q","enabled":True,"config":{"role":"demand","retries":0}})
    calls = {"n": 0}
    def fail(source, config, limit):
        calls["n"] += 1
        raise CollectorError("network unavailable")
    monkeypatch.setattr(sources_service, "_dispatch", fail)
    with pytest.raises(CollectorError):
        collect_source(sid)
    source = get_source(sid)
    assert source["failure_streak"] == 1
    assert source["next_retry_at"]
    second = collect_source(sid)
    assert second["status"] == "SKIPPED"
    assert "backoff" in second["reason"]
    assert calls["n"] == 1


def test_evidence_quality_and_time_quality_are_materialized():
    src = _source("quality", "v2ex", "demand")
    eid, _ = insert_evidence(src, _item("v2ex", "q1", "请问有没有排班Excel模板？", "门店想找可以自动排班并统计工时的Excel模板", "alice", True))
    with conn() as c:
        row = c.execute("SELECT evidence_quality_score,published_time_quality,demand_signal_score,content_fingerprint FROM evidence WHERE id=?", (eid,)).fetchone()
    assert row["evidence_quality_score"] >= 70
    assert row["published_time_quality"] == "source"
    assert row["demand_signal_score"] > 0
    assert len(row["content_fingerprint"]) == 64


def test_confidence_is_exposed_separately_from_trend_score():
    a = _source("a", "xiaohongshu", "demand", "agent_reach")
    b = _source("b", "reddit", "demand", "agent_reach")
    insert_evidence(a, _item("xiaohongshu", "1", "求门店排班Excel模板", "需要门店排班和工时统计Excel", "alice"))
    insert_evidence(b, _item("reddit", "2", "门店排班Excel模板求推荐", "需要门店排班和工时统计Excel工具", "bob"))
    build_from_unlinked_evidence()
    opp = list_opportunities()[0]
    assert 0 < opp["trend_confidence"] <= 100
    assert opp["trusted_trend_score"] <= opp["trend_score"]
    assert opp["data_quality_score"] > 0

def test_same_platform_item_id_with_changed_tracking_url_is_one_evidence():
    src = _source("identity", "xiaohongshu", "demand", "agent_reach")
    a = _item("xiaohongshu", "note-42", "求一个排班模板", "需要门店自动排班Excel", "alice")
    a.source_url = "https://www.xiaohongshu.com/explore/note-42?xsec_token=abc&utm_source=test"
    b = _item("xiaohongshu", "note-42", "求一个排班模板", "需要门店自动排班Excel", "alice")
    b.source_url = "https://www.xiaohongshu.com/explore/note-42?xsec_token=def&utm_source=other"
    eid1, n1 = insert_evidence(src, a)
    eid2, n2 = insert_evidence(src, b)
    assert eid1 == eid2
    assert n1 is True and n2 is False
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"] == 1
        assert c.execute("SELECT observation_count FROM evidence WHERE id=?", (eid1,)).fetchone()["observation_count"] == 2

def test_cross_language_business_concepts_cluster_without_paid_embeddings():
    xhs = _source("xhs-cn", "xiaohongshu", "demand", "agent_reach")
    reddit = _source("reddit-en", "reddit", "demand", "agent_reach")
    insert_evidence(xhs, _item("xiaohongshu", "cn1", "求一个小公司工资考勤Excel模板", "需要自动算工资和考勤的表格", "alice"))
    insert_evidence(reddit, _item("reddit", "en1", "Looking for a payroll attendance spreadsheet template", "Need an Excel spreadsheet for small business payroll and attendance", "bob"))
    result = build_from_unlinked_evidence()
    assert result["candidate_groups"] == 1
    opp = list_opportunities()[0]
    assert opp["platform_count"] == 2
    assert opp["evidence_count"] == 2

def test_v2ex_is_auxiliary_and_cannot_create_business_opportunity():
    sid = create_source({"name":"v2-low-value","source_type":"v2ex","platform":"v2ex","query":"q","enabled":True,"config":{"role":"demand"}})
    insert_evidence({"id": sid}, _item("v2ex", "v2-1", "求库存管理Excel模板", "需要库存出入库自动统计Excel", "alice"))
    result = build_from_unlinked_evidence()
    assert result["processed_evidence"] == 0
    assert list_opportunities() == []
