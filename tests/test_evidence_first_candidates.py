from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, init_db
from app.services.clustering import rebuild_clusters
from app.services.ingest import insert_evidence
from app.services.sources import create_source
from app.services.validation import validate_opportunity


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    old = settings.db_path
    settings.db_path = str(tmp_path / "evidence-first.db")
    init_db()
    yield
    settings.db_path = old


def test_high_quality_discussion_can_form_context_candidate_without_inflating_demand():
    source_id = create_source(
        {
            "name": "xhs-core-context",
            "source_type": "agent_reach",
            "platform": "xiaohongshu",
            "query": "办公效率",
            "enabled": True,
            "config": {"role": "demand", "priority": "P0", "business_group": "core_demand"},
        }
    )
    source = {"id": source_id}
    insert_evidence(
        source,
        CollectedItem(
            platform="xiaohongshu",
            source_type="agent_reach_xiaohongshu",
            source_item_id="ctx-1",
            source_url="https://www.xiaohongshu.com/explore/ctx-1",
            title="团队每周都在重复整理会议记录和任务进度",
            content="我们团队每周都会整理会议记录、行动项和项目进度，大家的格式都不一样，汇总起来很花时间。最近在讨论怎样把这套流程做得更统一、更省事。",
            author="real-user",
            published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            raw={"id": "ctx-1"},
        ),
    )
    with conn() as c:
        c.execute(
            "UPDATE evidence SET intent_class='discussion',intent_confidence=0.72,intent_weight=0,evidence_quality_score=82 WHERE source_item_id='ctx-1'"
        )

    result = rebuild_clusters()

    assert result["processed_evidence"] == 0
    assert result["context_evidence_considered"] == 1
    assert result["candidate_groups"] == 1
    opportunity_id = int(result["opportunity_ids"][0])
    with conn() as c:
        relation = c.execute(
            "SELECT relation_type FROM opportunity_evidence WHERE opportunity_id=?",
            (opportunity_id,),
        ).fetchone()["relation_type"]
        opportunity = c.execute(
            "SELECT evidence_count,explicit_demand_count,probable_demand_count FROM opportunities WHERE id=?",
            (opportunity_id,),
        ).fetchone()
    assert relation == "context"
    assert opportunity["evidence_count"] == 0
    assert opportunity["explicit_demand_count"] == 0
    assert opportunity["probable_demand_count"] == 0

    validation = validate_opportunity(opportunity_id)
    assert validation["status"] == "FAILED"
    assert "no_explicit_demand" in validation["reason_codes"]
