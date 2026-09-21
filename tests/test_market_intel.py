from __future__ import annotations

from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.collectors import marketplaces
from app.collectors.marketplaces import _creative_market_product_links, _etsy_listing_digital_status, _etsy_search_rows, _etsy_shop_sales, collect_etsy_market
from app.config import settings
from app.db import conn, init_db, now_iso
from app.services import sources
from app.services import market_intel
from app.services.market_intel import list_market_intel, market_intel_for_opportunity, market_intel_summary
from app.services.sources import collect_source, create_source


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    old = {
        "db_path": settings.db_path,
        "market_intel_match_threshold": settings.market_intel_match_threshold,
        "market_intel_high_heat_score": settings.market_intel_high_heat_score,
        "market_intel_high_sales_score": settings.market_intel_high_sales_score,
        "etsy_api_key": settings.etsy_api_key,
    }
    settings.db_path = str(tmp_path / "market-intel.db")
    settings.market_intel_match_threshold = 0.10
    settings.market_intel_high_heat_score = 65
    settings.market_intel_high_sales_score = 60
    settings.etsy_api_key = ""
    init_db()
    yield
    for key, value in old.items():
        setattr(settings, key, value)


def _market_source(name: str = "market-test") -> int:
    return create_source(
        {
            "name": name,
            "source_type": "etsy_market",
            "platform": "etsy",
            "query": "payroll excel template",
            "enabled": True,
            "config": {
                "role": "market",
                "priority": "P1",
                "business_group": "market_intel",
                "limit": 20,
            },
        }
    )


def _market_item(
    item_id: str,
    title: str,
    *,
    rank: int = 1,
    sales_count: int | None = None,
    sales_scope: str | None = None,
) -> CollectedItem:
    return CollectedItem(
        platform="etsy",
        source_type="etsy_market_intel",
        source_item_id=item_id,
        source_url=f"https://www.etsy.com/listing/{item_id}",
        title=title,
        author="high-volume-shop",
        engagement={},
        raw={
            "digital_product_verified": True,
            "digital_verification": "test_verified_digital",
            "market_metrics": {
                "rank_position": rank,
                "views_count": 25000,
                "favorites_count": 1800,
                "review_count": 420,
                "rating": 4.9,
                "sales_count": sales_count,
                "sales_scope": sales_scope,
                "sales_verified": sales_count is not None,
                "sales_metric_source_url": "https://www.etsy.com/shop/high-volume-shop" if sales_count is not None else None,
                "metric_source_url": "https://www.etsy.com/search?q=payroll",
            }
        },
    )


def test_market_source_writes_market_intel_not_demand_evidence(monkeypatch):
    source_id = _market_source()
    monkeypatch.setattr(
        sources,
        "_dispatch",
        lambda source, config, limit: [
            _market_item("1", "Payroll Excel Template", sales_count=10000, sales_scope="shop"),
        ],
    )

    result = collect_source(source_id, force=True)

    assert result["status"] == "SUCCESS"
    assert result["market_intel_sync"]["inserted"] == 1
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"] == 0
        assert c.execute("SELECT COUNT(*) c FROM market_intel_items").fetchone()["c"] == 1
        assert c.execute("SELECT COUNT(*) c FROM market_intel_snapshots").fetchone()["c"] == 1

    item = list_market_intel(10)[0]
    assert item["high_heat"] is True
    assert item["high_sales"] is True
    assert item["sales_verified"] is True
    assert item["sales_scope"] == "shop"
    assert item["sales_count"] == 10000


def test_missing_sales_never_becomes_fake_sales_score(monkeypatch):
    source_id = _market_source("market-no-sales")
    monkeypatch.setattr(
        sources,
        "_dispatch",
        lambda source, config, limit: [
            _market_item("2", "Popular Resume Template", rank=1, sales_count=None, sales_scope=None),
        ],
    )

    collect_source(source_id, force=True)
    item = list_market_intel(10)[0]

    assert item["high_heat"] is True
    assert item["sales_verified"] is False
    assert item["sales_count"] is None
    assert item["sales_score"] is None
    assert item["high_sales"] is False
    assert item["hot_and_selling"] is False


def test_market_intel_auto_matches_existing_opportunity(monkeypatch):
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO opportunities(title,candidate_key,normalized_need,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("Payroll Excel Template", "market-match-opportunity", "payroll excel template", "NEW", now, now),
        )
        opportunity_id = int(cur.lastrowid)
        c.execute("UPDATE opportunities SET cluster_active=1 WHERE id=?", (opportunity_id,))

    source_id = _market_source("market-match")
    monkeypatch.setattr(
        sources,
        "_dispatch",
        lambda source, config, limit: [
            _market_item("3", "Payroll Excel Template for Small Business", sales_count=12000, sales_scope="shop"),
        ],
    )
    result = collect_source(source_id, force=True)

    assert result["market_intel_sync"]["matched_links"] >= 1
    intel = market_intel_for_opportunity(opportunity_id)
    assert intel["matched_count"] == 1
    assert intel["high_heat_count"] == 1
    assert intel["high_sales_count"] == 1
    assert intel["items"][0]["sales_scope"] == "shop"

    summary = market_intel_summary()
    assert summary["total"] == 1
    assert summary["hot_and_selling"] == 1


def test_creative_market_parser_extracts_ranked_product_links():
    page = """
    <html><body>
      <a href="/studio/1001-Payroll-Workbook"><span>Payroll Workbook</span></a>
      <a href="/studio/1001-Payroll-Workbook">Payroll Workbook</a>
      <a href="/maker/1002-Resume-Template">Resume Template</a>
      <a href="/templates-themes/popular">Popular</a>
    </body></html>
    """
    rows = _creative_market_product_links(page, "https://creativemarket.com/templates-themes/popular", 10)
    assert rows == [
        ("https://creativemarket.com/studio/1001-Payroll-Workbook", "Payroll Workbook", "studio"),
        ("https://creativemarket.com/maker/1002-Resume-Template", "Resume Template", "maker"),
    ]


def test_etsy_parser_keeps_public_shop_sales_scope_separate_from_listing():
    page = """
    <script type="application/json">
    {"results":[{"listing_id":123,"title":"Notion Business Template","shop_name":"TemplateShop",
      "price":{"amount":1900,"divisor":100,"currency_code":"USD"},
      "views":2000,"num_favorers":350,"review_count":80,"rating":4.8}]}
    </script>
    """
    rows = _etsy_search_rows(page, 10)
    assert rows[0]["listing_id"] == "123"
    assert rows[0]["price"] == pytest.approx(19.0)
    assert rows[0]["favorites"] == pytest.approx(350)

    assert _etsy_shop_sales("<div>12,345 sales</div>") == 12345
    assert _etsy_shop_sales("<div>No public sales metric</div>") is None



def test_missing_rating_never_uses_neutral_fake_rating(monkeypatch):
    source_id = _market_source("market-no-rating")
    item = _market_item("4", "Resume Template Without Rating", rank=4, sales_count=None, sales_scope=None)
    item.raw["market_metrics"]["rating"] = None
    item.raw["market_metrics"]["review_count"] = 500
    monkeypatch.setattr(sources, "_dispatch", lambda source, config, limit: [item])

    collect_source(source_id, force=True)
    row = list_market_intel(10)[0]

    assert row["rating"] is None
    assert row["metric_availability"]["rating"] is False
    assert row["score_basis"] == "heat_only"


def test_sales_with_unknown_scope_is_not_converted_to_sales_score(monkeypatch):
    source_id = _market_source("market-unknown-sales-scope")
    item = _market_item("5", "Template With Ambiguous Sales Scope", rank=2, sales_count=5000, sales_scope="unknown")
    monkeypatch.setattr(sources, "_dispatch", lambda source, config, limit: [item])

    collect_source(source_id, force=True)
    row = list_market_intel(10)[0]

    assert row["sales_count"] == 5000
    assert row["sales_verified"] is True
    assert row["sales_scope"] == "unknown"
    assert row["sales_score"] is None
    assert row["high_sales"] is False
    assert row["score_basis"] == "heat_only"



def test_unverified_market_item_is_rejected_before_market_intel(monkeypatch):
    source_id = _market_source("market-unverified-physical")
    item = _market_item("6", "Physical Planner Book", rank=1)
    item.raw.pop("digital_product_verified", None)
    item.raw.pop("digital_verification", None)
    monkeypatch.setattr(sources, "_dispatch", lambda source, config, limit: [item])

    result = collect_source(source_id, force=True)

    assert result["market_intel_sync"]["inserted"] == 0
    assert result["market_intel_sync"]["rejected_invalid"] == 1
    assert list_market_intel(10) == []


def test_etsy_digital_status_requires_explicit_digital_evidence():
    digital_page = '<script type="application/json">{"listing_id":123,"is_digital":true}</script>'
    physical_page = '<script type="application/json">{"listing_id":123,"is_digital":false}</script>'
    unknown_page = '<html><body>Printable-looking product without a digital label</body></html>'

    assert _etsy_listing_digital_status(digital_page, "123")[0] is True
    assert _etsy_listing_digital_status(physical_page, "123")[0] is False
    assert _etsy_listing_digital_status(unknown_page, "123")[0] is None


def test_etsy_collection_keeps_only_verified_digital_listings(monkeypatch):
    search_page = """
    <script type="application/json">
    {"results":[
      {"listing_id":101,"title":"Physical Planner","is_digital":false,"price":{"amount":2000,"divisor":100,"currency_code":"USD"}},
      {"listing_id":102,"title":"Digital Payroll Spreadsheet","is_digital":true,"price":{"amount":1500,"divisor":100,"currency_code":"USD"}}
    ]}
    </script>
    """
    monkeypatch.setattr(marketplaces, "_get", lambda url: search_page)

    rows = collect_etsy_market("digital payroll template", limit=10)

    assert len(rows) == 1
    assert rows[0].source_item_id == "102"
    assert rows[0].raw["digital_product_verified"] is True
    assert "is_digital" in rows[0].raw["digital_verification"]



def test_market_similarity_matches_cross_language_semantic_aliases():
    from app.services import market_intel

    score = market_intel._similarity(
        "Excel 库存管理表格模板",
        "Need an inventory spreadsheet for a small business",
    )

    assert score >= settings.market_intel_match_threshold


def test_market_similarity_does_not_match_unrelated_single_template_term():
    from app.services import market_intel

    score = market_intel._similarity(
        "简历模板",
        "Need an inventory spreadsheet",
    )

    assert score < settings.market_intel_match_threshold



def test_targeted_market_queries_remove_generic_packaging_words():
    etsy_queries = market_intel.targeted_market_queries(
        "WhatsApp Automation Agency GTM Checklist & Stack Validator",
        "etsy",
    )
    xianyu_queries = market_intel.targeted_market_queries(
        "WhatsApp Automation Agency GTM Checklist & Stack Validator",
        "xianyu",
    )
    assert etsy_queries[0] == "WhatsApp Automation Agency GTM Checklist & Stack Validator"
    assert etsy_queries[1] == "WhatsApp Automation"
    assert xianyu_queries == ["WhatsApp 自动化", "WhatsApp 工作流"]


def test_targeted_market_validation_writes_market_intel_not_demand(monkeypatch):
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO opportunities(title,candidate_key,normalized_need,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (
                "WhatsApp Automation Agency GTM Checklist",
                "watch-market-1",
                "WhatsApp automation launch checklist",
                "NEW",
                now,
                now,
            ),
        )
        opportunity_id = int(cur.lastrowid)
        c.execute("UPDATE opportunities SET cluster_active=1 WHERE id=?", (opportunity_id,))

    def fake_etsy(query, limit=12):
        return [
            _market_item(
                "watch-etsy-1",
                "WhatsApp Automation Agency Checklist Template",
                rank=1,
                sales_count=9000,
                sales_scope="shop",
            )
        ]

    monkeypatch.setattr(market_intel, "collect_etsy_market", fake_etsy)
    monkeypatch.setattr(
        market_intel,
        "collect_xianyu_market",
        lambda query, limit=12: (_ for _ in ()).throw(RuntimeError("xianyu unavailable")),
    )

    result = market_intel.collect_targeted_market_intel(
        opportunity_id,
        "WhatsApp Automation Agency GTM Checklist & Stack Validator",
        per_query_limit=5,
    )

    assert result["market_changed"] is True
    assert result["after"]["matched_count"] >= 1
    assert result["after"]["high_heat_count"] >= 1
    assert result["after"]["high_sales_count"] >= 1
    assert any(item["platform"] == "xianyu" and item["status"] == "FAILED" for item in result["results"])
    assert result["validation_status"] == "PARTIAL"
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"] == 0
        source = c.execute(
            "SELECT enabled,config_json FROM sources WHERE name=?",
            (f"WATCH market validation #{opportunity_id} etsy",),
        ).fetchone()
    assert source["enabled"] == 0
    assert "watch_targeted_market_validation" in source["config_json"]



def test_targeted_market_empty_result_is_not_collector_failure():
    exc = RuntimeError(
        "opencli: ok: false\nerror:\n  code: EMPTY_RESULT\n  message: xianyu search returned no data"
    )
    assert market_intel._targeted_market_exception_status(exc) == "EMPTY"


def test_targeted_market_validation_status_distinguishes_partial_and_inconclusive():
    partial = market_intel._targeted_market_validation_status([
        {"platform": "etsy", "status": "FAILED"},
        {"platform": "xianyu", "status": "EMPTY"},
    ])
    assert partial == {
        "validation_status": "PARTIAL",
        "platform_status": {"etsy": "FAILED", "xianyu": "EMPTY"},
    }

    inconclusive = market_intel._targeted_market_validation_status([
        {"platform": "etsy", "status": "FAILED"},
        {"platform": "xianyu", "status": "FAILED"},
    ])
    assert inconclusive["validation_status"] == "INCONCLUSIVE"


def test_marketplace_http_collector_honors_trust_env_setting(monkeypatch):
    from app.collectors import marketplaces as marketplace_collectors

    captured = {}

    class FakeResponse:
        text = "<html></html>"
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url):
            return FakeResponse()

    old = settings.marketplace_trust_env
    settings.marketplace_trust_env = True
    monkeypatch.setattr(marketplace_collectors.httpx, "Client", FakeClient)
    try:
        marketplace_collectors._get("https://example.com")
    finally:
        settings.marketplace_trust_env = old

    assert captured["trust_env"] is True



def test_etsy_open_api_is_preferred_when_api_key_is_configured(monkeypatch):
    captured = {}

    class FakeResponse:
        url = "https://openapi.etsy.com/v3/application/listings/active?keywords=payroll"
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "count": 2,
                "results": [
                    {
                        "listing_id": 901,
                        "title": "Digital Payroll Workbook",
                        "type": "download",
                        "url": "https://www.etsy.com/listing/901",
                        "price": {"amount": 1900, "divisor": 100, "currency_code": "USD"},
                    },
                    {
                        "listing_id": 902,
                        "title": "Physical Payroll Binder",
                        "type": "physical",
                        "url": "https://www.etsy.com/listing/902",
                        "price": {"amount": 2900, "divisor": 100, "currency_code": "USD"},
                    },
                ],
            }

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    old_key = settings.etsy_api_key
    settings.etsy_api_key = "key:secret"
    monkeypatch.setattr(marketplaces.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        marketplaces,
        "_get",
        lambda url: (_ for _ in ()).throw(AssertionError("HTML fallback must not run")),
    )
    try:
        rows = collect_etsy_market("payroll workbook", limit=10)
    finally:
        settings.etsy_api_key = old_key

    assert captured["url"].endswith("/v3/application/listings/active")
    assert captured["params"]["keywords"] == "payroll workbook"
    assert captured["params"]["sort_on"] == "score"
    assert captured["client"]["headers"]["x-api-key"] == "key:secret"
    assert len(rows) == 1
    assert rows[0].source_item_id == "901"
    assert rows[0].raw["digital_verification"].startswith("etsy_open_api:")
    assert rows[0].raw["market_metrics"]["price"] == pytest.approx(19.0)
    assert rows[0].raw["market_metrics"]["sales_verified"] is False


def test_etsy_html_403_tells_operator_to_configure_api_key(monkeypatch):
    old_key = settings.etsy_api_key
    settings.etsy_api_key = ""
    monkeypatch.setattr(
        marketplaces,
        "_get",
        lambda url: (_ for _ in ()).throw(
            marketplaces.CollectorError("marketplace request failed: 403 Forbidden")
        ),
    )
    try:
        with pytest.raises(marketplaces.CollectorError, match="configure ETSY_API_KEY"):
            collect_etsy_market("whatsapp automation", limit=5)
    finally:
        settings.etsy_api_key = old_key
