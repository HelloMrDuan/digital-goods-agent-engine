from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.db import conn, init_db, now_iso
from app.services import product_builder
from app.services.llm import qiniu
from app.services.llm.budget import BudgetExceededError, budget_snapshot, reserve_ai_budget


@pytest.mark.parametrize("path", ["/tmp/a.py", "C:/outside.py", "../a.py", "a/../b.py", "a\\..\\b.py", "CON.txt", "a:N.txt", "a/.", "a//b.py", "a./b.py", "a/b.py ", "a/COM1.csv"])
def test_artifact_paths_are_portable_and_confined(path):
    with pytest.raises(ValueError, match="unsafe"):
        product_builder._safe_relative_path(path)


@pytest.mark.parametrize("files", [
    [{"path":"README.md","content":"guide"}, {"path":"README.md","content":"guide2"}],
    [{"path":"README.md","content":"guide"}, {"path":"manifest.json","content":"{}"}],
    [{"path":"README.md","content":"guide"}, {"path":"other.txt","content":"guide2"}],
])
def test_markdown_manifest_requires_distinct_primary_deliverable(files):
    with pytest.raises(ValueError):
        product_builder._validate_manifest({"files":files},"markdown")


@pytest.mark.parametrize("path,content", [("broken.json","{bad}"), ("broken.py","def f("), ("broken.csv","a,b\n1\n"), ("empty.csv","a,b\n")])
def test_invalid_structured_deliverables_are_not_written(tmp_path,path,content):
    with pytest.raises((ValueError,SyntaxError)):
        product_builder._write_text_file(tmp_path,{"path":path,"content":content})
    assert not (tmp_path/path).exists()


def test_header_only_workbook_is_rejected(tmp_path):
    with pytest.raises(ValueError,match="substantive"):
        product_builder._write_workbook(tmp_path,{"path":"empty.xlsx","sheets":[{"name":"Inputs","columns":["Name"],"rows":[]}]})


def test_usable_csv_and_workbook_can_be_reopened(tmp_path):
    import csv
    from openpyxl import load_workbook
    product_builder._write_text_file(tmp_path,{"path":"rates.csv","content":"Hours,Rate,Total\n8,20,160\n"})
    with (tmp_path/"rates.csv").open(encoding="utf-8",newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [{"Hours":"8","Rate":"20","Total":"160"}]
    product_builder._write_workbook(tmp_path,{"path":"payroll.xlsx","sheets":[{"name":"Pay","columns":["Hours","Rate","Total"],"rows":[[8,20,"=A2*B2"]]}]})
    wb=load_workbook(tmp_path/"payroll.xlsx")
    assert wb["Pay"]["C2"].value == "=A2*B2"
    assert wb["Pay"]["A2"].value * wb["Pay"]["B2"].value == 160
    wb.close()


@pytest.fixture
def budget_env(monkeypatch,tmp_path):
    monkeypatch.setattr(settings,"db_path",str(tmp_path/"budget.db"))
    monkeypatch.setattr(settings,"daily_ai_budget_cny",0.1)
    monkeypatch.setattr(settings,"qiniu_api_token","private-token-that-must-not-leak")
    monkeypatch.setattr(settings,"qiniu_min_request_interval_seconds",0)
    init_db()


def test_paid_invalid_json_keeps_usage_and_budget_charge(budget_env,monkeypatch):
    import httpx
    def response(*args):
        return httpx.Response(200,json={"choices":[{"message":{"content":"invalid json"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1000,"completion_tokens":500,"total_tokens":1500}})
    monkeypatch.setattr(qiniu,"_post_with_rate_limit_retry",response)
    with pytest.raises(qiniu.QiniuError):
        qiniu.chat_json_with_meta(operation="audit",entity_type="system",entity_id=None,payload={},max_new_tokens=800)
    with conn() as c:
        row=c.execute("SELECT * FROM ai_runs").fetchone()
    assert row["status"] == "FAILED"
    assert row["total_tokens"] == 1500
    assert row["estimated_cost_cny"] == pytest.approx(0.0032)
    assert budget_snapshot()["spent_cny"] == pytest.approx(0.0032)
    assert budget_snapshot()["reserved_cny"] == 0
    with pytest.raises(BudgetExceededError):
        reserve_ai_budget("Qiniu",0.099)


def test_unsettled_reservation_is_not_expired_or_ignored_at_midnight(budget_env):
    with conn() as c:
        c.execute("INSERT INTO ai_budget_reservations(provider,reserved_cost_cny,created_at) VALUES('Qiniu',0.09,'2026-01-01T00:00:00+00:00')")
    assert budget_snapshot()["reserved_cny"] == pytest.approx(0.09)
    with pytest.raises(BudgetExceededError):
        reserve_ai_budget("Qiniu",0.02)


def test_concurrent_budget_reservations_cannot_exceed_daily_cap(budget_env):
    from concurrent.futures import ThreadPoolExecutor
    def reserve(_):
        try:
            return reserve_ai_budget("Qiniu",0.06)
        except BudgetExceededError:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(reserve,range(6)))
    assert sum(result is not None for result in results) == 1
    assert budget_snapshot()["reserved_cny"] == pytest.approx(0.06)


def test_model_timeout_keeps_reservation_and_redacts_secret(budget_env,monkeypatch):
    import httpx
    from app.security import redact_error
    def timeout(*args):
        raise httpx.ReadTimeout("Authorization: Bearer "+settings.qiniu_api_token)
    monkeypatch.setattr(qiniu,"_post_with_rate_limit_retry",timeout)
    with pytest.raises(qiniu.QiniuError) as caught:
        qiniu.chat_json_with_meta(operation="audit",entity_type="system",entity_id=None,payload={},max_new_tokens=800)
    with conn() as c:
        error=c.execute("SELECT error FROM ai_runs").fetchone()["error"]
    assert settings.qiniu_api_token not in error
    assert settings.qiniu_api_token not in str(caught.value)
    assert "[REDACTED]" in redact_error("Bearer another-private-token")
    assert budget_snapshot()["reserved_cny"] > 0


@pytest.mark.parametrize("path", ["/api/xianyu/orders/123", "/api/deliveries", "/api/products/1/artifact/download"])
def test_buyer_data_and_downloads_require_auth_even_for_reads(monkeypatch,path):
    from app.main import _write_auth_error
    monkeypatch.setattr(settings,"api_auth_token","secret-token")
    assert _write_auth_error("GET",path,"10.0.0.8",None,None)[0] == 401
    assert _write_auth_error("GET",path,"10.0.0.8","Bearer secret-token",None) is None


def test_paid_workflow_snapshot_deduplication_and_explicit_retry(budget_env):
    from app.services.paid_workflows import run_paid_once
    calls=[]
    def paid():
        calls.append(1)
        return {"card_id":len(calls)}
    assert run_paid_once("agents",1,"same",paid)["card_id"] == 1
    assert run_paid_once("agents",1,"same",paid)["idempotent"] is True
    assert len(calls) == 1
    assert run_paid_once("agents",1,"changed",paid)["card_id"] == 2
    assert run_paid_once("agents",1,"same",paid,force=True)["card_id"] == 3


def test_paid_workflow_claim_is_concurrent_and_failure_is_persisted(budget_env):
    from app.services.paid_workflows import run_paid_once
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    def paid():
        entered.set()
        assert release.wait(10)
        raise RuntimeError("provider failure")
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(run_paid_once,"agents",1,"same",paid)
        try:
            assert entered.wait(10)
            with pytest.raises(ValueError,match="RUNNING"):
                run_paid_once("agents",1,"changed",lambda: {"unexpected":True})
        finally:
            release.set()
        with pytest.raises(RuntimeError):
            first.result()
    with pytest.raises(ValueError,match="explicit force"):
        run_paid_once("agents",1,"same",lambda: {"unexpected":True})


def test_v1133_nonempty_database_migrates_twice_without_data_loss(monkeypatch,tmp_path):
    import sqlite3
    database=tmp_path/"legacy.db"
    with sqlite3.connect(database) as db:
        db.executescript(Path("tests/fixtures/v1133_schema.sql").read_text(encoding="utf-8"))
        db.execute("INSERT INTO sources(id,name,source_type,platform,created_at,updated_at) VALUES(1,'fixture legacy source','github','github',?,?)",(now_iso(),now_iso()))
        db.execute("""INSERT INTO evidence(id,source_id,platform,source_type,title,collected_at,raw_json,raw_hash,canonical_key)
                      VALUES(1,1,'github','github','fixture legacy need',?,'{}','fixture-hash','fixture-key')""",(now_iso(),))
        db.execute("INSERT INTO opportunities(id,title,candidate_key,created_at,updated_at) VALUES(1,'fixture need','fixture-key',?,?)",(now_iso(),now_iso()))
        db.execute("INSERT INTO products(id,opportunity_id,sku,name,status,created_at,updated_at) VALUES(1,1,'FIXTURE','fixture product','DRAFT',?,?)",(now_iso(),now_iso()))
        db.execute("INSERT INTO deliveries(product_id,platform,order_ref,status,created_at,updated_at) VALUES(1,'manual','fixture-order','PENDING',?,?)",(now_iso(),now_iso()))
    monkeypatch.setattr(settings,"db_path",str(database))
    init_db()
    init_db()
    with conn() as c:
        assert c.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM evidence_sources").fetchone()[0] == 1
        assert c.execute("SELECT sku FROM products").fetchone()[0] == "FIXTURE"
        assert c.execute("SELECT order_ref FROM deliveries").fetchone()[0] == "fixture-order"
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_core_api_routes_have_unique_method_path_pairs():
    from collections import Counter
    from app.main import app
    routes=Counter((method,route.path) for route in app.routes for method in getattr(route,"methods",[]) if route.path.startswith("/api/"))
    assert {key:count for key,count in routes.items() if count > 1} == {}


def test_sensitive_read_guard_is_installed_on_actual_api(budget_env,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings,"api_auth_token","secret-token")
    client=TestClient(app)
    response=client.get("/api/deliveries")
    assert response.status_code == 401
    authenticated=client.get("/api/deliveries",headers={"Authorization":"Bearer secret-token"})
    assert authenticated.status_code == 200
    assert authenticated.json() == []


def test_partial_refresh_remains_partial_without_ai(monkeypatch):
    from app import routes_v12
    updates=[]
    monkeypatch.setattr(routes_v12,"run_demand_discovery",lambda **kwargs:{"status":"PARTIAL"})
    monkeypatch.setattr(routes_v12,"_refresh_market_intelligence",lambda **kwargs:{"failed":0,"summary":{}})
    monkeypatch.setattr(routes_v12,"build_from_unlinked_evidence",lambda:{})
    monkeypatch.setattr(routes_v12,"_validate_recent_demand_candidates",lambda **kwargs:{"passed":0,"processed":0})
    monkeypatch.setattr(routes_v12,"_update_rebuild",lambda task_id,**kwargs:updates.append(kwargs))
    routes_v12._run_rebuild(1,False,1,True,1)
    assert updates[-1]["status"] == "PARTIAL"
