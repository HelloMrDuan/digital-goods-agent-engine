from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.collectors import agent_reach as ar
from app.config import settings
from app.db import conn, init_db, loads
from app.services.ingest import content_fingerprint, insert_evidence
from app.services.intent import classify_intent
from app.services.sources import create_source
from app.services.clustering import rebuild_clusters


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    old = settings.db_path
    settings.db_path = str(tmp_path / "intent.db")
    init_db()
    yield
    settings.db_path = old


def _source(name="p0", platform="reddit"):
    sid = create_source({
        "name": name,
        "source_type": "agent_reach",
        "platform": platform,
        "query": "real demand",
        "enabled": True,
        "config": {"role": "demand", "priority": "P0", "business_group": "core_demand"},
    })
    return {"id": sid}


def test_real_reddit_request_is_explicit_demand():
    d = classify_intent(
        "Need a Budget Spreadsheet – Any Recommendations?",
        "I’m looking for a budget Excel sheet or template. Does anyone know where I can download one?",
        "post",
    )
    assert d.label == "explicit_demand"
    assert d.demand_weight == 1.0


def test_real_reddit_followup_requests_are_demand_but_offers_are_not():
    assert classify_intent("", "Hi, would you mind sharing your spreadsheet?", "comment").label == "explicit_demand"
    assert classify_intent("", "can you PM me a link to it for google sheets?", "comment").label == "explicit_demand"
    assert classify_intent("", "I need something that will auto categorize for me", "comment").label in {"explicit_demand", "probable_demand"}
    assert classify_intent("", "I made my own for my wife and I, looks like this", "comment").label in {"supply", "showcase"}
    assert classify_intent("", "DM me if you need a stupid simple spreadsheet!", "comment").label == "supply"


def test_xiaohongshu_supply_showcase_does_not_become_demand():
    samples = [
        "超全面｜9000套Excel表格模板",
        "Excel表格模板免费分享",
        "我做的财务收支明细表，老板都夸我！",
        "上班无聊做了个excel动态日历",
    ]
    labels = [classify_intent(x, "", "post").label for x in samples]
    assert all(x not in {"explicit_demand", "probable_demand"} for x in labels)


def test_same_comment_wording_from_different_authors_is_not_collapsed():
    a = CollectedItem(platform="reddit", source_type="agent_reach_reddit_comment", source_item_id="a", source_url="https://reddit/thread", title="Can I get a copy?", content="Can I get a copy?", author="alice", raw={"id":"a"}, content_kind="comment", parent_source_item_id="p1", thread_url="https://reddit/thread")
    b = CollectedItem(platform="reddit", source_type="agent_reach_reddit_comment", source_item_id="b", source_url="https://reddit/thread", title="Can I get a copy?", content="Can I get a copy?", author="bob", raw={"id":"b"}, content_kind="comment", parent_source_item_id="p1", thread_url="https://reddit/thread")
    assert content_fingerprint(a) != content_fingerprint(b)


def test_supply_inside_p0_source_is_stored_but_excluded_from_cluster():
    src = _source(platform="xiaohongshu")
    insert_evidence(src, CollectedItem(platform="xiaohongshu", source_type="agent_reach_xiaohongshu", source_item_id="s1", source_url="https://xhs/s1", title="Excel表格模板免费分享", content="我整理了9000套模板", author="seller", raw={"id":"s1"}))
    result = rebuild_clusters()
    assert result["processed_evidence"] == 0
    with conn() as c:
        row = c.execute("SELECT intent_class FROM evidence WHERE source_item_id='s1'").fetchone()
        assert row["intent_class"] in {"supply", "showcase"}


def test_comment_request_inside_supply_thread_can_build_demand_cluster():
    src = _source(platform="reddit")
    insert_evidence(src, CollectedItem(platform="reddit", source_type="agent_reach_reddit", source_item_id="p1", source_url="https://reddit/p1", title="Financial Statement Template", content="Here it is, I made my own template", author="maker", raw={"id":"p1"}))
    insert_evidence(src, CollectedItem(platform="reddit", source_type="agent_reach_reddit_comment", source_item_id="c1", source_url="https://reddit/p1", title="Can I get a copy?", content="Can I get a copy?", author="buyer", raw={"id":"c1"}, content_kind="comment", parent_source_item_id="p1", thread_url="https://reddit/p1"))
    result = rebuild_clusters()
    assert result["processed_evidence"] == 1
    assert result["candidate_groups"] == 1
    with conn() as c:
        opp = c.execute("SELECT explicit_demand_count,comment_demand_count FROM opportunities WHERE cluster_active=1").fetchone()
        assert opp["explicit_demand_count"] == 1
        assert opp["comment_demand_count"] == 1


def test_reddit_read_parser_creates_real_comment_items(monkeypatch):
    parent = CollectedItem(platform="reddit", source_type="agent_reach_reddit", source_item_id="1n5gp77", source_url="https://www.reddit.com/r/budget/comments/1n5gp77/x/", title="Need a Budget Spreadsheet", content="I'm looking for a budget spreadsheet", author="op", published_at="2026-08-01T00:00:00+00:00", raw={"id":"1n5gp77"})
    monkeypatch.setattr(ar.shutil, "which", lambda name: "opencli" if name == "opencli" else None)
    monkeypatch.setattr(ar, "_run", lambda cmd, timeout=180: [
        {"author":"op","text":"Need a Budget Spreadsheet","type":"POST","score":18},
        {"author":"a","text":"I made my own, looks like this","type":"L0","score":4},
        {"author":"b","text":"Hi, would you mind sharing your spreadsheet?","type":"L1","score":1},
    ])
    items = ar._enrich_opencli("reddit", [parent], 1, 10)
    assert len(items) == 2
    assert all(x.content_kind == "comment" for x in items)
    assert all(x.thread_url == parent.source_url for x in items)
    assert items[0].published_time_quality == "inherited_thread"


def test_xhs_comment_command_uses_full_signed_url(monkeypatch):
    signed = "https://www.xiaohongshu.com/search_result/abc?xsec_token=REAL_SIGNED_TOKEN&xsec_source=pc_search"
    parent = CollectedItem(platform="xiaohongshu", source_type="agent_reach_xiaohongshu", source_item_id="abc", source_url=signed, title="Excel模板免费分享", raw={"id":"abc"}, published_at="2026-08-30")
    seen = []
    monkeypatch.setattr(ar.shutil, "which", lambda name: "opencli" if name == "opencli" else None)
    def fake_run(cmd, timeout=180):
        seen.append(cmd)
        return [{"author":"u1","text":"求一份，可以分享吗？","id":"c1","likes":2}]
    monkeypatch.setattr(ar, "_run", fake_run)
    items = ar._enrich_opencli("xiaohongshu", [parent], 1, 10)
    assert seen[0][3] == signed
    assert len(items) == 1
    assert items[0].source_url == signed
    assert items[0].content_kind == "comment"
