from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, init_db
from app.services.clustering import trend_detail
from app.services.ingest import insert_evidence
from app.services.opportunities import build_from_unlinked_evidence, get_evidence_for_opportunity, list_opportunities
from app.services.sensenova import SenseNovaError, chat_json
from app.services.sources import create_source


@pytest.fixture(autouse=True)
def temp_db():
    old = settings.db_path
    old_threshold = settings.cluster_similarity_threshold
    with tempfile.TemporaryDirectory() as td:
        settings.db_path = str(Path(td) / "test.db")
        settings.cluster_similarity_threshold = 0.24
        init_db()
        yield
    settings.db_path = old
    settings.cluster_similarity_threshold = old_threshold


def _source(name: str, platform: str, role: str = "demand") -> dict:
    sid = create_source({"name":name,"source_type":"agent_reach" if platform in {"xiaohongshu","reddit"} else "v2ex","platform":platform,"query":"需求采集","enabled":True,"config":{"role":role}})
    return {"id": sid}


def test_invalid_evidence_without_source_identity_is_rejected():
    source_id = create_source({"name":"real-rss","source_type":"rss","platform":"rss","url":"https://example.com/feed","enabled":True,"config":{"role":"demand"}})
    source = {"id": source_id}
    item = CollectedItem(platform="rss", source_type="rss", title="真实标题", raw={"title":"真实标题"})
    with pytest.raises(ValueError):
        insert_evidence(source, item)


def test_opportunity_is_built_only_from_stored_evidence():
    source = _source("xhs-demand", "xiaohongshu")
    item = CollectedItem(
        platform="xiaohongshu", source_type="agent_reach_xiaohongshu", source_item_id="note-123",
        source_url="https://www.xiaohongshu.com/explore/note-123", title="求一个工资计算Excel模板",
        content="想找一个能自动算工资和考勤的Excel模板", raw={"id":"note-123","title":"求一个工资计算Excel模板"},
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    eid, inserted = insert_evidence(source, item)
    assert eid and inserted
    result = build_from_unlinked_evidence()
    assert result["processed_evidence"] == 1
    opps = list_opportunities()
    assert len(opps) == 1
    ev = get_evidence_for_opportunity(opps[0]["id"])
    assert len(ev) == 1
    assert ev[0]["source_item_id"] == "note-123"


def test_duplicate_real_item_becomes_observation_not_fake_demand_row():
    source = _source("xhs-observe", "xiaohongshu")
    item = CollectedItem(
        platform="xiaohongshu", source_type="agent_reach_xiaohongshu", source_item_id="same-1",
        source_url="https://www.xiaohongshu.com/explore/same-1", title="求工资核算Excel",
        content="需要工资考勤自动核算", engagement={"likes": 5}, raw={"id":"same-1","likes":5},
    )
    eid1, new1 = insert_evidence(source, item)
    item.engagement = {"likes": 8}
    item.raw = {"id":"same-1","likes":8}
    eid2, new2 = insert_evidence(source, item)
    assert eid1 == eid2
    assert new1 is True and new2 is False
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"] == 1
        assert c.execute("SELECT COUNT(*) c FROM evidence_observations").fetchone()["c"] == 2
        assert c.execute("SELECT observation_count FROM evidence WHERE id=?",(eid1,)).fetchone()["observation_count"] == 2


def test_supply_only_evidence_never_becomes_user_demand_cluster():
    source_id = create_source({"name":"github-supply","source_type":"github","platform":"github","query":"automation templates","enabled":True,"config":{"role":"supply"}})
    source = {"id":source_id}
    insert_evidence(source, CollectedItem(
        platform="github",source_type="github_repositories",source_item_id="repo1",source_url="https://github.com/acme/repo",
        title="Payroll Excel Automation Template",content="An open source project",raw={"id":"repo1"},
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    ))
    result = build_from_unlinked_evidence()
    assert result["processed_evidence"] == 0
    assert list_opportunities() == []


def test_cross_platform_similar_real_demand_is_clustered_and_trended():
    xhs = _source("xhs-payroll", "xiaohongshu")
    v2 = _source("reddit-payroll", "reddit")
    now = datetime.now(timezone.utc)
    insert_evidence(xhs, CollectedItem(
        platform="xiaohongshu",source_type="agent_reach_xiaohongshu",source_item_id="x1",source_url="https://xhs.example/x1",
        title="求工资考勤自动计算Excel模板",content="小公司需要工资考勤自动计算，最好Excel直接出工资表",raw={"id":"x1"},
        published_at=(now-timedelta(days=1)).isoformat(timespec="seconds"),
    ))
    insert_evidence(v2, CollectedItem(
        platform="reddit",source_type="agent_reach_reddit",source_item_id="v1",source_url="https://www.reddit.com/comments/v1/",
        title="有没有工资考勤自动计算的Excel表",content="想找工资考勤自动核算表格，小团队用",raw={"id":"v1"},
        published_at=(now-timedelta(days=2)).isoformat(timespec="seconds"),
    ))
    result = build_from_unlinked_evidence()
    assert result["candidate_groups"] == 1
    assert result["cross_platform_groups"] == 1
    opp = list_opportunities()[0]
    assert opp["evidence_count"] == 2
    assert opp["platform_count"] == 2
    assert opp["current_7d"] == 2
    assert opp["previous_7d"] == 0
    assert opp["growth_7d_pct"] is None
    trend = trend_detail(opp["id"])
    assert trend and trend["is_new"] is True
    assert trend["platform_breakdown"]["xiaohongshu"] == 1
    assert trend["platform_breakdown"]["reddit"] == 1


def test_week_over_week_growth_uses_real_timestamps():
    source = _source("reddit-growth", "reddit")
    now = datetime.now(timezone.utc)
    dates = [now-timedelta(days=2), now-timedelta(days=4), now-timedelta(days=9)]
    for i, dt in enumerate(dates):
        insert_evidence(source, CollectedItem(
            platform="reddit",source_type="agent_reach_reddit",source_item_id=f"g{i}",source_url=f"https://www.reddit.com/comments/g{i}/",
            title=f"求工资考勤自动计算Excel模板 {i}",content="小团队需要工资考勤自动核算Excel表格",raw={"id":f"g{i}"},published_at=dt.isoformat(timespec="seconds")
        ))
    build_from_unlinked_evidence()
    opp = list_opportunities()[0]
    assert opp["current_7d"] == 2
    assert opp["previous_7d"] == 1
    assert round(float(opp["growth_7d_pct"]), 2) == 100.0


def test_sensenova_has_no_mock_fallback():
    old_token, old_model = settings.sensenova_api_token, settings.sensenova_model
    settings.sensenova_api_token = ""
    settings.sensenova_model = ""
    try:
        with pytest.raises(SenseNovaError, match="No mock fallback"):
            chat_json(operation="test", entity_type="test", entity_id=None, system="x", payload={"x":1})
    finally:
        settings.sensenova_api_token, settings.sensenova_model = old_token, old_model


def test_database_starts_with_zero_business_data():
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"] == 0
        assert c.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"] == 0
        assert c.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0
