from __future__ import annotations

from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.collectors import xianyu
from app.config import settings
from app.db import conn, init_db
from app.services import sources
from app.services.market_intel import list_market_intel
from app.services.sources import collect_source, create_source


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path):
    old = {
        "db_path": settings.db_path,
        "market_intel_match_threshold": settings.market_intel_match_threshold,
    }
    settings.db_path = str(tmp_path / "xianyu.db")
    settings.market_intel_match_threshold = 0.1
    init_db()
    yield
    for key, value in old.items():
        setattr(settings, key, value)


def test_xianyu_market_keeps_only_detail_verified_digital_goods(monkeypatch):
    candidates = [
        CollectedItem(
            platform="xianyu",
            source_type="agent_reach_xianyu",
            source_item_id="1001",
            source_url="https://www.goofish.com/item?categoryId=0&id=1001",
            title="Excel工资模板电子版",
            content="虚拟商品，无需物流，电子文件在线交付",
            author="seller-a",
            raw={
                "price": "19.90",
                "want_count": 88,
                "_xianyu_detail_verified": True,
                "_xianyu_detail": {"title": "Excel工资模板电子版", "description": "虚拟商品，无需物流，电子文件在线交付"},
            },
        ),
        CollectedItem(
            platform="xianyu",
            source_type="agent_reach_xianyu",
            source_item_id="1002",
            source_url="https://www.goofish.com/item?categoryId=0&id=1002",
            title="实体记账本",
            content="全新实物，快递发货",
            author="seller-b",
            raw={
                "price": "29.90",
                "_xianyu_detail_verified": True,
                "_xianyu_detail": {"title": "实体记账本", "description": "全新实物，快递发货"},
            },
        ),
    ]
    monkeypatch.setattr(xianyu, "collect_agent_reach", lambda *args, **kwargs: candidates)

    rows = xianyu.collect_xianyu_market("Excel模板 电子版", 10)

    assert len(rows) == 1
    assert rows[0].source_item_id == "1001"
    assert rows[0].raw["digital_product_verified"] is True
    assert rows[0].raw["market_metrics"]["price"] == pytest.approx(19.9)
    assert rows[0].raw["market_metrics"]["want_count"] == pytest.approx(88)


def test_xianyu_market_accepts_real_world_netdisk_delivery_wording(monkeypatch):
    candidate = CollectedItem(
        platform="xianyu",
        source_type="agent_reach_xianyu",
        source_item_id="987594951873",
        source_url="https://www.goofish.com/item?id=987594951873",
        title="手机可编辑Word简历模板",
        content="Word简历模板，手机也可以直接编辑",
        author="通通一米阳光",
        raw={
            "price": "¥1",
            "want_count": "2205",
            "_xianyu_detail_verified": True,
            "_xianyu_detail": {
                "description": "直接购买，拍下夸克网盘发货的 标价即售价，可以直接拍！",
                "browse_count": "15016",
                "collect_count": "153",
            },
        },
    )
    monkeypatch.setattr(xianyu, "collect_agent_reach", lambda *args, **kwargs: [candidate])

    rows = xianyu.collect_xianyu_market("简历模板", 10)

    assert len(rows) == 1
    assert rows[0].raw["digital_verification"] == "xianyu_detail_marker:网盘发货"
    assert rows[0].raw["market_metrics"]["want_count"] == pytest.approx(2205)
    assert rows[0].raw["market_metrics"]["views_count"] == pytest.approx(15016)
    assert rows[0].raw["market_metrics"]["favorites_count"] == pytest.approx(153)


def test_xianyu_market_source_enters_market_intel_not_demand(monkeypatch):
    source_id = create_source(
        {
            "name": "xianyu-test",
            "source_type": "xianyu_market",
            "platform": "xianyu",
            "query": "Excel模板 电子版",
            "enabled": True,
            "config": {"role": "market", "priority": "P1", "business_group": "market_intel"},
        }
    )
    monkeypatch.setattr(
        sources,
        "_dispatch",
        lambda source, config, limit: [
            CollectedItem(
                platform="xianyu",
                source_type="xianyu_market_intel",
                source_item_id="2001",
                source_url="https://www.goofish.com/item?categoryId=0&id=2001",
                title="自动工资Excel电子模板",
                content="电子版，无需物流",
                author="seller-c",
                raw={
                    "digital_product_verified": True,
                    "digital_verification": "xianyu_detail_marker:电子版",
                    "market_metrics": {
                        "rank_position": 1,
                        "price": 9.9,
                        "currency": "CNY",
                        "want_count": 120,
                        "sales_verified": False,
                    },
                },
            )
        ],
    )

    result = collect_source(source_id, force=True)

    assert result["status"] == "SUCCESS"
    assert result["market_intel_sync"]["inserted"] == 1
    assert list_market_intel(10)[0]["marketplace"] == "xianyu"
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"] == 0
