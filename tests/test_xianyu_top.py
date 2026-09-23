from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.config import settings
from app.db import conn, dumps, init_db, loads, now_iso
from app.services import xianyu_top


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.text = str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class _TopClient:
    def __init__(self, order_status: int = 2, virtual_success: bool = True):
        self.order_status = order_status
        self.virtual_success = virtual_success
        self.calls = []

    def post(self, url, **kwargs):
        data = kwargs["data"]
        self.calls.append((url, data))
        method = data["method"]
        if method == xianyu_top.ORDER_QUERY_METHOD:
            return _Response({
                "alibaba_idle_isv_order_query_response": {
                    "module": {
                        "biz_order_id": data["biz_order_id"],
                        "order_status": self.order_status,
                        "buyer_nick": "buyer",
                        "payment": 2900,
                        "x_global_biz_code": "virtual",
                    }
                }
            })
        if method == xianyu_top.VIRTUAL_DELIVERY_METHOD:
            return _Response({
                "alibaba_idle_isv_goosefish_virtual_delivery_response": {
                    "result": {"data": self.virtual_success}
                }
            })
        raise AssertionError(method)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path):
    old = {
        "db_path": settings.db_path,
        "artifact_dir": settings.artifact_dir,
        "xianyu_top_app_key": settings.xianyu_top_app_key,
        "xianyu_top_app_secret": settings.xianyu_top_app_secret,
        "xianyu_top_access_token": settings.xianyu_top_access_token,
        "xianyu_top_biz_code": settings.xianyu_top_biz_code,
        "xianyu_top_gateway": settings.xianyu_top_gateway,
    }
    settings.db_path = str(tmp_path / "xianyu-top.db")
    settings.artifact_dir = str(tmp_path / "generated")
    settings.xianyu_top_app_key = "app-key"
    settings.xianyu_top_app_secret = "secret"
    settings.xianyu_top_access_token = "access-token"
    settings.xianyu_top_biz_code = "virtual"
    settings.xianyu_top_gateway = "https://gw.api.taobao.com/router/rest"
    init_db()
    yield
    for key, value in old.items():
        setattr(settings, key, value)


def _approved_product(tmp_path: Path) -> int:
    now = now_iso()
    bundle_dir = Path(settings.artifact_dir) / "product_1"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle = bundle_dir / "DG-XIANYU-TOP.zip"
    bundle.write_bytes(b"real generated product bytes")
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()

    with conn() as c:
        source_id = c.execute(
            """INSERT INTO sources(name,source_type,platform,query,config_json,enabled,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("demand", "agent_reach", "reddit", "need template", dumps({"role":"demand"}), 1, "READY", now, now),
        ).lastrowid
        evidence_id = c.execute(
            """INSERT INTO evidence(
                   source_id,platform,source_type,source_item_id,source_url,title,content,author,published_at,
                   collected_at,raw_json,raw_hash,canonical_key,evidence_quality_score,published_time_quality,
                   demand_signal_score,content_kind,intent_class,intent_confidence,intent_weight
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source_id, "reddit", "agent_reach_reddit", "e1", "https://reddit.com/e1",
                "Need payroll workbook", "I need a payroll workbook", "buyer", now, now,
                dumps({"id":"e1"}), "rawhash", "canonical", 90, "source", 90, "post",
                "explicit_demand", 0.95, 1.0,
            ),
        ).lastrowid
        opportunity_id = c.execute(
            """INSERT INTO opportunities(
                   title,candidate_key,normalized_need,status,overall_score,copyright_risk,evidence_count,
                   cluster_active,analysis_stale,validation_status,validation_score,validation_snapshot_hash,
                   validated_at,validation_json,analysis_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Payroll Workbook", "opp", "payroll workbook", "PUBLISHED", 88, 10, 1,
                1, 0, "PASSED", 90, "snap", now,
                dumps({"status":"PASSED"}), dumps({"decision":"BUILD"}), now, now,
            ),
        ).lastrowid
        c.execute(
            "INSERT INTO opportunity_evidence(opportunity_id,evidence_id,relation_type) VALUES(?,?,?)",
            (opportunity_id, evidence_id, "demand"),
        )
        validation_id = c.execute(
            """INSERT INTO commercial_validation_runs(
                   opportunity_id,status,score,policy_version,snapshot_hash,metrics_json,reasons_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (opportunity_id, "PASSED", 90, "test", "snap", dumps({}), dumps([]), now),
        ).lastrowid
        product_id = c.execute(
            """INSERT INTO products(
                   opportunity_id,sku,name,category,audience,price_suggestion,artifact_format,title,description,
                   keywords_json,deliverables_json,faq_json,compliance_notes,status,gate_status,gate_score,
                   gate_snapshot_hash,gate_checked_at,gate_json,approved_at,approved_gate_snapshot_hash,
                   price_currency,published_at,published_platform,external_listing_id,external_listing_url,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id, "DG-XIANYU-TOP", "Payroll Workbook", "office", "teams", 29, "xlsx",
                "Payroll Workbook", "Digital workbook", dumps(["payroll"]), dumps(["xlsx"]),
                dumps([{"q":"q","a":"a"}]), "original", "PUBLISHED", "PASS", 100,
                "gatehash", now, dumps({}), now, "gatehash", "CNY", now, "xianyu",
                "99999", "https://www.goofish.com/item?categoryId=0&id=99999", now, now,
            ),
        ).lastrowid
        c.execute(
            """INSERT INTO product_builds(
                   product_id,status,bundle_path,checksum,manifest_json,source_asset_id,error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (product_id, "SUCCESS", str(bundle), checksum, dumps({"files":["payroll.xlsx"]}), None, None, now, now),
        )

    # Use the real gate code contract without rebuilding the lengthy fixture:
    # current approval validation is patched in these focused TOP tests.
    return int(product_id)


def test_top_md5_signature_is_sorted_and_uppercase():
    params = {"b": "2", "a": "1"}
    expected = hashlib.md5("secreta1b2secret".encode("utf-8")).hexdigest().upper()
    assert xianyu_top._top_sign(params, "secret") == expected


def test_order_query_uses_real_top_method_and_returns_paid_status():
    client = _TopClient(order_status=2)
    order = xianyu_top.xianyu_order_query("3318740388015865620", client=client)

    assert order["order_status"] == 2
    assert order["order_status_name"] == "PAID"
    assert client.calls[0][0] == "https://gw.api.taobao.com/router/rest"
    sent = client.calls[0][1]
    assert sent["method"] == xianyu_top.ORDER_QUERY_METHOD
    assert sent["biz_order_id"] == "3318740388015865620"
    assert sent["sign_method"] == "md5"
    assert sent["sign"]


def test_virtual_delivery_refuses_unpaid_order(monkeypatch, tmp_path):
    product_id = _approved_product(tmp_path)
    monkeypatch.setattr(xianyu_top, "require_approved_current_product", lambda pid: {
        "id": pid, "opportunity_id": 1, "external_listing_id": "99999"
    })
    monkeypatch.setattr(xianyu_top, "latest_successful_bundle", lambda pid: {"checksum":"abc"})
    client = _TopClient(order_status=1)

    with pytest.raises(ValueError, match="requires a PAID order"):
        xianyu_top.xianyu_virtual_delivery(product_id, "123456789", client=client)

    assert [call[1]["method"] for call in client.calls] == [xianyu_top.ORDER_QUERY_METHOD]
    with conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM deliveries").fetchone()["c"] == 0


def test_paid_order_calls_official_virtual_delivery_and_records_truthful_status(monkeypatch, tmp_path):
    product_id = _approved_product(tmp_path)
    monkeypatch.setattr(xianyu_top, "require_approved_current_product", lambda pid: {
        "id": pid, "opportunity_id": 1, "external_listing_id": "99999"
    })
    monkeypatch.setattr(xianyu_top, "latest_successful_bundle", lambda pid: {"checksum":"abc"})
    client = _TopClient(order_status=2, virtual_success=True)

    result = xianyu_top.xianyu_virtual_delivery(product_id, "123456789", client=client)

    assert result["status"] == "VIRTUAL_SHIPPED"
    assert result["shipment_confirmed"] is True
    assert result["file_transferred"] is False
    methods = [call[1]["method"] for call in client.calls]
    assert methods == [xianyu_top.ORDER_QUERY_METHOD, xianyu_top.VIRTUAL_DELIVERY_METHOD]
    payload = client.calls[1][1]["idle_open_isv_virtual_delivery_param"]
    assert json.loads(payload)["biz_order_id"] == "123456789"

    with conn() as c:
        row = c.execute("SELECT status,delivery_type,resource_json FROM deliveries WHERE order_ref='123456789'").fetchone()
    assert row["status"] == "VIRTUAL_SHIPPED"
    assert row["delivery_type"] == "xianyu_virtual_top"
    resource = loads(row["resource_json"], {})
    assert resource["shipment_confirmed"] is True
    assert resource["file_transferred"] is False


def test_top_missing_credentials_is_explicit(monkeypatch):
    monkeypatch.setattr(settings, "xianyu_top_access_token", "")
    with pytest.raises(ValueError, match="not configured"):
        xianyu_top.xianyu_order_query("123456789", client=_TopClient())
