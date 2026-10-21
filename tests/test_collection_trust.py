from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.collectors import agent_reach as ar
from app.collectors.base import CollectedItem, CollectorError
from app.config import settings
from app.db import conn, dumps, init_db, loads
from app.evidence_identity import evidence_assessment
from app.services import demand_discovery as dd
from app.services.ingest import backfill_intents, insert_evidence
from app.services.intent import classify_intent, INTENT_POLICY_VERSION
from app.services.sources import create_source
from app.services.validation import _metrics


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    previous = settings.db_path
    settings.db_path = str(tmp_path / "trust.db")
    init_db()
    yield
    settings.db_path = previous


def source():
    return {"id": create_source({"name": "trust-test", "platform": "reddit", "source_type": "agent_reach", "query": "template", "config": {"role": "demand", "business_group": "core_demand"}})}


def item(**kwargs):
    values = dict(platform="reddit", source_type="agent_reach_reddit", source_item_id="post1",
                  source_url="https://www.reddit.com/comments/post1/", title="Need a budget spreadsheet",
                  content="I need a spreadsheet to track expenses for my household.", author="buyer",
                  published_at=datetime.now(timezone.utc).isoformat(), raw={"id": "post1"})
    return CollectedItem(**(values | kwargs))


@pytest.mark.parametrize("title,body,label", [
    ("Usage-based deals: six terms that need a field in CPQ", "Disclosure: I’m building a tool for the arithmetic layer. No product yet.", "supply"),
    ("Six terms that need a field on a quote template", "These are advice for sales operations teams.", "discussion"),
    ("[For Hire] Supplier catalogue PDF into your spreadsheet template — £15 pilot", "", "supply"),
    ("求多人表格模板", "", "explicit_demand"),
    ("帮忙做excel表格有偿！", "", "explicit_demand"),
    ("Cataloguing DVDs on an excel spreadsheet - looking for template ideas!", "I am cataloguing my 1000 movies and need an Excel template.", "explicit_demand"),
])
def test_buyer_direction_regressions(title, body, label):
    decision = classify_intent(title, body)
    assert decision.label == label
    if not body and label == "explicit_demand":
        assert decision.confidence <= .75
        assert "title_only_requires_body_verification" in decision.reason_codes


def test_comments_sharing_thread_url_do_not_overwrite_parent_or_each_other():
    src = source()
    parent = item()
    pid, _ = insert_evidence(src, parent)
    c1 = replace(parent, source_item_id="post1", content_kind="comment", author="alice", title="Can I get a copy?", content="Can I get a copy?", parent_source_item_id="post1")
    c2 = replace(c1, source_item_id="comment2", author="bob")
    a, _ = insert_evidence(src, c1)
    b, _ = insert_evidence(src, c2)
    assert len({pid, a, b}) == 3
    assert insert_evidence(src, c1) == (a, False)
    with conn() as c:
        assert c.execute("SELECT content_kind FROM evidence WHERE id=?", (pid,)).fetchone()[0] == "post"
        assert c.execute("SELECT count(*) FROM evidence").fetchone()[0] == 3


def test_enriched_body_is_persisted_and_survives_later_search_only_observation():
    src = source()
    parent = item(content=None)
    eid, _ = insert_evidence(src, parent)
    full = replace(parent, content="I need an expense tracker spreadsheet for my own household.",
                   raw={"id": "post1", "_detail": {"text": "real source body"}, "_enrichment": {"detail_status": "SUCCESS"}})
    assert insert_evidence(src, full) == (eid, False)
    insert_evidence(src, parent)
    with conn() as c:
        row = dict(c.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone())
    assert row["content"] == full.content
    assert loads(row["raw_json"], {})["_detail"] == full.raw["_detail"]
    assert loads(row["raw_json"], {})["_body_cached_from_previous_observation"] is True
    assert evidence_assessment(row)["body_present"] is True


def test_concurrent_probes_observe_one_row_without_losing_observations():
    src = source()
    post = item()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: insert_evidence(src, post), range(12)))
    assert sum(inserted for _, inserted in results) == 1
    assert len({eid for eid, _ in results}) == 1
    with conn() as c:
        assert c.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM evidence_observations").fetchone()[0] == 12


def test_xhs_enrichment_backfills_body_and_keeps_errors_visible(monkeypatch):
    parent = item(platform="xiaohongshu", source_item_id=None, source_url="https://www.xiaohongshu.com/explore/abc123?xsec_token=secret&xsec_source=search", content=None)
    monkeypatch.setattr(ar.shutil, "which", lambda name: "opencli")
    def run(cmd, timeout):
        if cmd[2] == "note":
            return [{"field": "content", "value": "求一份能自动统计每月支出的表格，自己家庭使用。"}]
        raise CollectorError("failed at ?xsec_token=secret&xsec_source=search")
    monkeypatch.setattr(ar, "_run", run)
    assert ar._enrich_opencli("xiaohongshu", [parent], 1, 3) == []
    assert parent.content
    assert parent.source_item_id == "abc123"
    state = parent.raw["_enrichment"]
    assert state["detail_status"] == "SUCCESS"
    assert state["comments_status"] == "FAILED"
    assert "secret" not in state["comments_error"]


def test_xhs_unexpected_schema_and_comment_time_do_not_become_verified(monkeypatch):
    parent = item(platform="xiaohongshu", source_url="https://www.xiaohongshu.com/explore/abc123?xsec_token=secret")
    monkeypatch.setattr(ar.shutil, "which", lambda name: "opencli")
    monkeypatch.setattr(ar, "_run", lambda *args, **kwargs: [{"message": "login required"}])
    ar._enrich_opencli("xiaohongshu", [parent], 1, 0)
    assert parent.raw["_enrichment"]["detail_status"] == "FAILED"
    comment = ar._comment_item("xiaohongshu", parent, {"text": "求一份表格模板", "author": "a", "time": "10-20"})
    assert comment.published_time_quality == "inherited_thread"
    assert comment.raw["time"] == "10-20"


def test_reddit_read_separates_title_from_body_and_rejects_wrong_author(monkeypatch):
    parent = item(content=None)
    monkeypatch.setattr(ar.shutil, "which", lambda name: "opencli")
    monkeypatch.setattr(ar, "_run", lambda *args, **kwargs: [{"type": "POST", "author": "buyer", "text": parent.title + "\n\nI need an expense tracker for my own use."}])
    ar._enrich_opencli("reddit", [parent], 1, 0)
    assert parent.content == "I need an expense tracker for my own use."
    monkeypatch.setattr(ar, "_run", lambda *args, **kwargs: [{"type": "POST", "author": "unrelated", "text": "Another post\n\nWrong body"}])
    ar._enrich_opencli("reddit", [parent], 1, 0)
    assert parent.raw["_enrichment"]["detail_status"] == "FAILED"
    assert "Wrong body" not in parent.content


def test_discovery_reports_partial_when_details_fail_even_if_search_hits_are_old(monkeypatch):
    dd.install_sources()
    old = item(published_at="2020-01-01", raw={"_enrichment": {"detail_status": "FAILED", "detail_error": "Navigation rejected"}})
    monkeypatch.setattr(dd, "collect_agent_reach", lambda *a, **k: [old])
    result = dd._collect_probe(dd._PROBES[2], queries=["query"])
    assert result["status"] == "PARTIAL"
    assert result["retained_recent"] == 0
    assert result["enrichment_failures"] == 1


def test_manual_source_collection_keeps_partial_detail_failure(monkeypatch):
    from app.services import sources
    src = source()
    with conn() as c:
        c.execute("UPDATE sources SET enabled=1 WHERE id=?", (src["id"],))
    post = item(raw={"_enrichment": {"comments_status": "FAILED", "comments_error": "Navigation rejected"}})
    monkeypatch.setattr(sources, "_dispatch", lambda *a, **k: [post])
    result = sources.collect_source(src["id"], force=True)
    assert result["status"] == "PARTIAL"
    with conn() as c:
        row = c.execute("SELECT status,error FROM collection_runs WHERE id=?", (result["run_id"],)).fetchone()
    assert row["status"] == "PARTIAL"
    assert "Navigation rejected" in row["error"]


def metric_row(id=1, **kwargs):
    return dict(id=id, platform="reddit", title="Need a template", content="I need an expense spreadsheet.", author="buyer",
                raw_json="{}", intent_class="explicit_demand", published_at=datetime.now(timezone.utc).isoformat(),
                published_time_quality="source", evidence_quality_score=90, content_kind="post") | kwargs


def test_gate_counts_accounts_instead_of_posts_and_rejects_title_only():
    metrics = _metrics([metric_row(), metric_row(2), metric_row(3, content=None, author="other")])
    assert metrics["effective_signal_count"] == 1
    assert metrics["title_only_or_missing_body_count"] == 1
    assert metrics["observed_demand_count"] == 3


def test_renamed_xhs_profile_is_one_account_and_display_name_is_unverified():
    rows = [metric_row(1, platform="xiaohongshu", author="old-name", raw_json=dumps({"author_url": "https://www.xiaohongshu.com/user/profile/abc"})),
            metric_row(2, platform="xiaohongshu", author="new-name", raw_json=dumps({"userId": "abc"})),
            metric_row(3, platform="xiaohongshu", author="display-only")]
    metrics = _metrics(rows)
    assert metrics["effective_signal_count"] == 1
    assert metrics["unverified_account_count"] == 1


def test_inherited_thread_date_does_not_establish_recent_comment_demand():
    metrics = _metrics([metric_row(content_kind="comment", published_time_quality="inherited_thread")])
    assert metrics["recent_30d_count"] == 0
    assert metrics["timestamp_coverage_pct"] == 0


def test_intent_policy_backfill_reclassifies_old_labeled_rows_once():
    src = source()
    eid, _ = insert_evidence(src, item(title="[For Hire] Excel template", content="I am offering spreadsheet services."))
    with conn() as c:
        c.execute("UPDATE evidence SET intent_class='explicit_demand',intent_policy_version='' WHERE id=?", (eid,))
    assert backfill_intents()["updated"] == 1
    assert backfill_intents()["updated"] == 0
    with conn() as c:
        row = c.execute("SELECT intent_class,intent_policy_version FROM evidence WHERE id=?", (eid,)).fetchone()
    assert tuple(row) == ("supply", INTENT_POLICY_VERSION)


def test_discovery_deduplicates_across_queries_and_surfaces_partial_enrichment(monkeypatch):
    monkeypatch.setattr(dd, "agent_reach_doctor", lambda: {"opencli": {"connected": True}})
    def collect(platform, query, **kwargs):
        assert kwargs["enrich"] is True
        return [item(platform=platform, raw={"_enrichment": {"detail_status": "FAILED", "detail_error": "not available"}})]
    monkeypatch.setattr(dd, "collect_agent_reach", collect)
    monkeypatch.setattr(dd, "_run_dynamic_corroboration", lambda *a, **k: {"queries": 0, "observed_evidence_ids": []})
    report = dd.run_demand_discovery(max_queries_per_probe=2)
    assert report["status"] == "PARTIAL"
    assert report["retained_recent"] == 2
    assert report["retained_occurrences"] == 4
    assert report["direct_demand_signals"] == 2
    assert all(p["enrichment_failures"] for p in report["probes"])
