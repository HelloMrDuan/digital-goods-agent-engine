from __future__ import annotations

from typing import Any

from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict, stable_hash
from .asset_registry import product_asset_readiness
from .opportunities import get_opportunity
from .validation import get_validation


POLICY_VERSION = "product-gate-v1"


def _decode_product_row(row: Any) -> dict[str, Any] | None:
    data = row_to_dict(row)
    if not data:
        return None
    data["keywords"] = loads(data.pop("keywords_json", None), []) or []
    data["deliverables"] = loads(data.pop("deliverables_json", None), []) or []
    data["faq"] = loads(data.pop("faq_json", None), []) or []
    data["gate"] = loads(data.pop("gate_json", None), None)
    return data


def get_product(product_id: int) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    return _decode_product_row(row)


def list_delivery_records(limit: int = 100) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM deliveries ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = row_to_dict(row) or {}
        data["resource"] = loads(data.pop("resource_json", None), None)
        result.append(data)
    return result


def _product_snapshot(
    product: dict[str, Any],
    opportunity: dict[str, Any],
    validation: dict[str, Any],
    asset_readiness: dict[str, Any],
) -> str:
    return stable_hash(
        POLICY_VERSION,
        product.get("id"),
        product.get("sku"),
        product.get("name"),
        product.get("title"),
        product.get("description"),
        product.get("price_suggestion"),
        dumps(product.get("keywords") or []),
        dumps(product.get("deliverables") or []),
        dumps(product.get("faq") or []),
        product.get("compliance_notes"),
        opportunity.get("analysis_json"),
        opportunity.get("analysis_stale"),
        opportunity.get("copyright_risk"),
        validation.get("snapshot_hash"),
        validation.get("current_snapshot_hash"),
        validation.get("status"),
        validation.get("is_current"),
        asset_readiness.get("snapshot_hash"),
        asset_readiness.get("decision"),
    )


def _nonempty_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def evaluate_gate_b(product_id: int, *, persist: bool = True) -> dict[str, Any]:
    product = get_product(product_id)
    if not product:
        raise KeyError("product not found")
    opportunity = get_opportunity(int(product["opportunity_id"]))
    if not opportunity:
        raise KeyError("opportunity not found")
    validation = get_validation(int(product["opportunity_id"]))
    asset_readiness = product_asset_readiness(product_id)

    blockers: list[str] = []
    review_flags: list[str] = []
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, ok: bool, *, severity: str, reason: str) -> None:
        checks[name] = {"ok": bool(ok), "severity": severity, "reason": None if ok else reason}
        if ok:
            return
        if severity == "BLOCKER":
            blockers.append(reason)
        else:
            review_flags.append(reason)

    record(
        "commercial_validation_current",
        validation.get("status") == "PASSED" and bool(validation.get("is_current")),
        severity="BLOCKER",
        reason="commercial_validation_not_current_passed",
    )
    record(
        "opportunity_analysis_current",
        opportunity.get("overall_score") is not None and not bool(opportunity.get("analysis_stale")),
        severity="BLOCKER",
        reason="opportunity_analysis_missing_or_stale",
    )
    record(
        "copyright_risk",
        int(opportunity.get("copyright_risk") or 0) < settings.high_risk_threshold,
        severity="BLOCKER",
        reason="high_copyright_or_compliance_risk",
    )
    record("sku", _nonempty_text(product.get("sku")), severity="BLOCKER", reason="missing_sku")
    record("name", _nonempty_text(product.get("name")), severity="BLOCKER", reason="missing_product_name")
    record("title", _nonempty_text(product.get("title")), severity="BLOCKER", reason="missing_listing_title")
    record(
        "description",
        _nonempty_text(product.get("description")),
        severity="BLOCKER",
        reason="missing_listing_description",
    )
    try:
        price = float(product.get("price_suggestion") or 0)
    except (TypeError, ValueError):
        price = 0.0
    record("price", price > 0, severity="BLOCKER", reason="missing_or_invalid_price")
    record(
        "deliverables",
        bool(product.get("deliverables")),
        severity="BLOCKER",
        reason="missing_deliverables",
    )
    record(
        "faq",
        bool(product.get("faq")),
        severity="REVIEW",
        reason="customer_service_knowledge_missing",
    )
    record(
        "compliance_notes",
        _nonempty_text(product.get("compliance_notes")),
        severity="REVIEW",
        reason="compliance_notes_missing",
    )
    if asset_readiness["decision"] == "BLOCKED":
        record(
            "asset_license_readiness",
            False,
            severity="BLOCKER",
            reason=(asset_readiness.get("blockers") or ["asset_license_blocked"])[0],
        )
    elif asset_readiness["decision"] == "REVIEW_REQUIRED":
        record(
            "asset_license_readiness",
            False,
            severity="REVIEW",
            reason=(asset_readiness.get("review_flags") or ["asset_license_review_required"])[0],
        )
    else:
        record("asset_license_readiness", True, severity="BLOCKER", reason="asset_license_not_ready")

    if blockers:
        decision = "BLOCKED"
    elif review_flags:
        decision = "REVIEW_REQUIRED"
    else:
        decision = "PASS"

    passed_checks = sum(1 for item in checks.values() if item["ok"])
    score = round(passed_checks / max(1, len(checks)) * 100.0, 2)
    snapshot_hash = _product_snapshot(product, opportunity, validation, asset_readiness)
    checked_at = now_iso()
    result = {
        "product_id": product_id,
        "opportunity_id": int(product["opportunity_id"]),
        "decision": decision,
        "score": score,
        "policy_version": POLICY_VERSION,
        "snapshot_hash": snapshot_hash,
        "checks": checks,
        "blockers": blockers,
        "review_flags": review_flags,
        "commercial_validation": {
            "status": validation.get("status"),
            "is_current": bool(validation.get("is_current")),
            "snapshot_hash": validation.get("snapshot_hash"),
        },
        "asset_readiness": asset_readiness,
        "checked_at": checked_at,
        "data_policy": "deterministic product readiness gate; no synthetic fulfillment or license claims",
    }

    if persist:
        with conn() as c:
            c.execute(
                """INSERT INTO product_gate_runs(
                       product_id,decision,score,policy_version,snapshot_hash,checks_json,blockers_json,review_flags_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    product_id,
                    decision,
                    score,
                    POLICY_VERSION,
                    snapshot_hash,
                    dumps(checks),
                    dumps(blockers),
                    dumps(review_flags),
                    checked_at,
                ),
            )
            c.execute(
                """UPDATE products SET gate_status=?,gate_score=?,gate_snapshot_hash=?,gate_checked_at=?,gate_json=?,updated_at=?
                   WHERE id=?""",
                (decision, score, snapshot_hash, checked_at, dumps(result), checked_at, product_id),
            )
    return result


def get_gate_b(product_id: int) -> dict[str, Any]:
    product = get_product(product_id)
    if not product:
        raise KeyError("product not found")
    opportunity = get_opportunity(int(product["opportunity_id"]))
    if not opportunity:
        raise KeyError("opportunity not found")
    validation = get_validation(int(product["opportunity_id"]))
    asset_readiness = product_asset_readiness(product_id)
    current_snapshot_hash = _product_snapshot(product, opportunity, validation, asset_readiness)

    with conn() as c:
        row = c.execute(
            "SELECT * FROM product_gate_runs WHERE product_id=? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
    if not row:
        return {
            "product_id": product_id,
            "decision": "NOT_RUN",
            "is_current": False,
            "current_snapshot_hash": current_snapshot_hash,
        }
    result = row_to_dict(row) or {}
    result["checks"] = loads(result.pop("checks_json", None), {}) or {}
    result["blockers"] = loads(result.pop("blockers_json", None), []) or []
    result["review_flags"] = loads(result.pop("review_flags_json", None), []) or []
    result["is_current"] = result.get("snapshot_hash") == current_snapshot_hash
    result["current_snapshot_hash"] = current_snapshot_hash
    return result


def approve_product(product_id: int) -> dict[str, Any]:
    product = get_product(product_id)
    if not product:
        raise KeyError("product not found")
    validation = get_validation(int(product["opportunity_id"]))
    if validation.get("status") != "PASSED" or not validation.get("is_current"):
        raise ValueError("current commercial validation must pass before product approval")
    result = evaluate_gate_b(product_id, persist=True)
    if result["decision"] != "PASS":
        reasons = result["blockers"] or result["review_flags"]
        raise ValueError(f"product Gate B must PASS before approval: {','.join(reasons)}")
    approved_at = now_iso()
    with conn() as c:
        row = c.execute("SELECT opportunity_id FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise KeyError("product not found")
        opportunity_id = int(row["opportunity_id"])
        c.execute(
            """UPDATE products SET status='APPROVED',approved_at=?,approved_gate_snapshot_hash=?,updated_at=?
               WHERE id=?""",
            (approved_at, result["snapshot_hash"], approved_at, product_id),
        )
        c.execute(
            "UPDATE opportunities SET status='READY_TO_PUBLISH',updated_at=? WHERE id=?",
            (approved_at, opportunity_id),
        )
    return {"id": product_id, "status": "APPROVED", "gate_b": result, "approved_at": approved_at}


def require_approved_current_product(product_id: int) -> dict[str, Any]:
    product = get_product(product_id)
    if not product:
        raise KeyError("product not found")
    if str(product.get("status") or "").upper() != "APPROVED":
        raise ValueError("product must be APPROVED before fulfillment")
    gate = get_gate_b(product_id)
    if gate.get("decision") != "PASS" or not gate.get("is_current"):
        raise ValueError("current product Gate B must pass before fulfillment")
    if product.get("approved_gate_snapshot_hash") != gate.get("snapshot_hash"):
        raise ValueError("product changed after approval; human re-approval is required before fulfillment")
    product["gate_b"] = gate
    return product
