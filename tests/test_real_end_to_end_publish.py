from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, dumps, init_db, now_iso
from app.services import product_builder, publishing
from app.services.ingest import insert_evidence
from app.services.opportunities import build_from_unlinked_evidence, list_opportunities
from app.services.product_lifecycle import approve_product, evaluate_gate_b
from app.services.sources import create_source
from app.services.validation import validate_opportunity


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path):
    old = {
        "db_path": settings.db_path,
        "artifact_dir": settings.artifact_dir,
        "cluster_similarity_threshold": settings.cluster_similarity_threshold,
        "commercial_validation_min_signals": settings.commercial_validation_min_signals,
        "commercial_validation_min_authors": settings.commercial_validation_min_authors,
        "commercial_validation_min_quality": settings.commercial_validation_min_quality,
        "commercial_validation_min_timestamp_coverage": settings.commercial_validation_min_timestamp_coverage,
        "commercial_validation_min_score": settings.commercial_validation_min_score,
        "etsy_api_key": settings.etsy_api_key,
        "etsy_access_token": settings.etsy_access_token,
        "etsy_shop_id": settings.etsy_shop_id,
        "etsy_taxonomy_id": settings.etsy_taxonomy_id,
        "etsy_shop_currency": settings.etsy_shop_currency,
        "etsy_listing_quantity": settings.etsy_listing_quantity,
    }
    settings.db_path = str(tmp_path / "flow.db")
    settings.artifact_dir = str(tmp_path / "generated")
    settings.cluster_similarity_threshold = 0.20
    settings.commercial_validation_min_signals = 2
    settings.commercial_validation_min_authors = 2
    settings.commercial_validation_min_quality = 55
    settings.commercial_validation_min_timestamp_coverage = 50
    settings.commercial_validation_min_score = 65
    settings.etsy_api_key = "key:secret"
    settings.etsy_access_token = "123.oauth-token"
    settings.etsy_shop_id = "456"
    settings.etsy_taxonomy_id = 1
    settings.etsy_shop_currency = "CNY"
    settings.etsy_listing_quantity = 999
    init_db()
    yield
    for key, value in old.items():
        setattr(settings, key, value)


def _ready_product() -> int:
    source_id = create_source(
        {
            "name": "real-flow-demand",
            "source_type": "agent_reach",
            "platform": "reddit",
            "query": "payroll workbook",
            "enabled": True,
            "config": {"role": "demand", "business_group": "core_demand", "priority": "P0"},
        }
    )
    for index, author in enumerate(("buyer-a", "buyer-b"), start=1):
        insert_evidence(
            {"id": source_id},
            CollectedItem(
                platform="reddit",
                source_type="agent_reach_reddit",
                source_item_id=f"real-{index}",
                source_url=f"https://www.reddit.com/comments/real-{index}/",
                title="Need a payroll attendance workbook template",
                content="I need an Excel workbook that calculates payroll, overtime and attendance for a small team.",
                author=author,
                published_at=(datetime.now(timezone.utc) - timedelta(days=index)).isoformat(timespec="seconds"),
                engagement={"score": 4},
                raw={"id": f"real-{index}", "author": author},
            ),
        )
    build_from_unlinked_evidence()
    opportunity = list_opportunities()[0]
    opportunity_id = int(opportunity["id"])
    assert validate_opportunity(opportunity_id)["status"] == "PASSED"
    now = now_iso()
    with conn() as c:
        c.execute(
            """UPDATE opportunities SET status='ANALYZED',overall_score=88,copyright_risk=10,
                      analysis_json=?,analysis_stale=0,updated_at=? WHERE id=?""",
            (
                dumps(
                    {
                        "decision": "BUILD",
                        "reasoning": "real demand from two independent buyers",
                        "pain_points": ["manual payroll and overtime calculation"],
                        "target_users": ["small teams"],
                        "primary_product": "Payroll Workbook",
                        "recommended_products": [{"name": "Payroll Workbook", "format": "xlsx", "why": "matches demand", "deliverables": ["xlsx", "guide"]}],
                        "price_range": {"currency": "CNY", "min": 20, "max": 50, "reason": "AI judgement"},
                    }
                ),
                now,
                opportunity_id,
            ),
        )
        cur = c.execute(
            """INSERT INTO products(
                   opportunity_id,sku,name,category,audience,price_suggestion,price_currency,artifact_format,title,description,
                   keywords_json,deliverables_json,faq_json,compliance_notes,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id,
                "DG-FLOW-001",
                "Payroll Workbook",
                "office automation",
                "small teams",
                29.0,
                "CNY",
                "xlsx",
                "Payroll and Overtime Workbook",
                "Original workbook for attendance, overtime and payroll tracking.",
                dumps(["payroll", "attendance"]),
                dumps(["Excel workbook", "buyer instructions"]),
                dumps([{"q": "Can I edit it?", "a": "Yes, edit the input sheets."}]),
                "Original generated content only.",
                "DRAFT",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def _artifact_response():
    return {
        "files": [
            {"path": "README.md", "content": "# Payroll Workbook\nEnter employee attendance in the workbook and review payroll totals."},
            {"path": "USAGE.md", "content": "1. Open payroll.xlsx\n2. Fill employee and attendance rows\n3. Review totals."},
        ],
        "workbooks": [
            {
                "path": "payroll.xlsx",
                "sheets": [
                    {
                        "name": "Employees",
                        "columns": ["Employee", "HourlyRate", "Hours", "OvertimeHours"],
                        "rows": [["Alice", 20, 160, 8]],
                    },
                    {
                        "name": "Summary",
                        "columns": ["Employee", "BasePay", "OvertimePay"],
                        "rows": [["Alice", "=Employees!B2*Employees!C2", "=Employees!B2*1.5*Employees!D2"]],
                    },
                ],
            }
        ],
        "quality_notes": ["Workbook and usage instructions are included."],
    }


def test_real_flow_builds_downloadable_package_and_self_owned_asset(monkeypatch):
    product_id = _ready_product()
    monkeypatch.setattr(product_builder, "chat_json", lambda **kwargs: _artifact_response())

    build = product_builder.build_product_artifact(product_id)

    bundle = Path(build["bundle_path"])
    assert bundle.is_file()
    assert bundle.suffix == ".zip"
    assert build["checksum"]
    assert "payroll.xlsx" in build["files"]
    with conn() as c:
        asset = c.execute(
            """SELECT sa.ownership_type,sa.commercial_use_allowed,sa.redistribution_allowed,pa.role
               FROM product_assets pa JOIN source_assets sa ON sa.id=pa.source_asset_id
               WHERE pa.product_id=? AND sa.id=?""",
            (product_id, build["source_asset_id"]),
        ).fetchone()
    assert asset["ownership_type"] == "self_owned"
    assert asset["commercial_use_allowed"] == 1
    assert asset["redistribution_allowed"] == 1
    assert asset["role"] == "deliverable"
    assert evaluate_gate_b(product_id)["decision"] == "PASS"


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.text = str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class _FakeEtsyClient:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/listings"):
            return _FakeResponse({"listing_id": 987654321})
        if url.endswith("/files"):
            return _FakeResponse({"listing_file_id": 11})
        if url.endswith("/images"):
            return _FakeResponse({"listing_image_id": 22})
        raise AssertionError(url)

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        assert url.endswith("/listings/987654321")
        assert kwargs["data"]["state"] == "active"
        return _FakeResponse({"listing_id": 987654321, "state": "active"})


def test_approved_real_package_routes_to_etsy_digital_listing(monkeypatch):
    product_id = _ready_product()
    monkeypatch.setattr(product_builder, "chat_json", lambda **kwargs: _artifact_response())
    product_builder.build_product_artifact(product_id)
    assert evaluate_gate_b(product_id)["decision"] == "PASS"
    assert approve_product(product_id)["status"] == "APPROVED"

    fake = _FakeEtsyClient()
    result = publishing.publish_to_etsy(product_id, client=fake)

    assert result["status"] == "SUCCESS"
    assert result["state"] == "active"
    assert result["external_id"] == "987654321"
    methods = [call[0] for call in fake.calls]
    assert methods == ["POST", "POST", "POST", "PUT"]
    create_payload = fake.calls[0][2]["data"]
    assert create_payload["type"] == "download"
    assert create_payload["title"] == "Payroll and Overtime Workbook"
    assert create_payload["price"] == "29.00"
    assert fake.calls[1][1].endswith("/files")
    assert fake.calls[2][1].endswith("/images")

    with conn() as c:
        product = c.execute(
            "SELECT status,published_platform,external_listing_id,external_listing_url FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    assert product["status"] == "PUBLISHED"
    assert product["published_platform"] == "etsy"
    assert product["external_listing_id"] == "987654321"
    assert product["external_listing_url"].endswith("/987654321")


def test_publish_refuses_missing_real_etsy_credentials(monkeypatch):
    product_id = _ready_product()
    monkeypatch.setattr(product_builder, "chat_json", lambda **kwargs: _artifact_response())
    product_builder.build_product_artifact(product_id)
    evaluate_gate_b(product_id)
    approve_product(product_id)
    settings.etsy_access_token = ""

    with pytest.raises(ValueError, match="not configured"):
        publishing.publish_to_etsy(product_id, client=_FakeEtsyClient())


def test_publish_refuses_currency_mismatch_without_explicit_price(monkeypatch):
    product_id = _ready_product()
    monkeypatch.setattr(product_builder, "chat_json", lambda **kwargs: _artifact_response())
    product_builder.build_product_artifact(product_id)
    evaluate_gate_b(product_id)
    approve_product(product_id)
    settings.etsy_shop_currency = "USD"

    with pytest.raises(ValueError, match="differs from Etsy shop currency"):
        publishing.publish_to_etsy(product_id, client=_FakeEtsyClient())



class _FakeXianyuBridge:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        assert url.endswith("/xianyu/publish")
        payload = kwargs["json"]
        assert payload["price"] == 29.0
        assert payload["condition"] == "全新"
        assert payload["category"] == "办公模板"
        assert payload["images_base64"]
        assert "虚拟数字商品" in payload["description"]
        return _FakeResponse({"status": "SUCCESS", "result": {"item_id": 1234567890123}})


def test_approved_real_package_publishes_to_real_xianyu_bridge(monkeypatch):
    product_id = _ready_product()
    monkeypatch.setattr(product_builder, "chat_json", lambda **kwargs: _artifact_response())
    product_builder.build_product_artifact(product_id)
    assert evaluate_gate_b(product_id)["decision"] == "PASS"
    assert approve_product(product_id)["status"] == "APPROVED"

    monkeypatch.setattr(settings, "agent_reach_bridge_url", "http://host.docker.internal:8765")
    monkeypatch.setattr(settings, "xianyu_publish_category", "办公模板")
    monkeypatch.setattr(settings, "xianyu_publish_condition", "全新")
    fake = _FakeXianyuBridge()

    result = publishing.publish_to_xianyu(product_id, client=fake)

    assert result["status"] == "SUCCESS"
    assert result["platform"] == "xianyu"
    assert result["external_id"] == "1234567890123"
    assert result["external_url"].endswith("id=1234567890123")
    with conn() as c:
        product = c.execute(
            "SELECT status,published_platform,external_listing_id FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    assert product["status"] == "PUBLISHED"
    assert product["published_platform"] == "xianyu"
    assert product["external_listing_id"] == "1234567890123"
