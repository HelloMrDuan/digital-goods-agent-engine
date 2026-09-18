from __future__ import annotations

from typing import Any

from ..db import conn, dumps, loads, now_iso, row_to_dict, stable_hash


LICENSE_POLICY_VERSION = "asset-license-v1"


def _decode_asset(row: Any) -> dict[str, Any] | None:
    data = row_to_dict(row)
    if not data:
        return None
    for key in ("commercial_use_allowed", "redistribution_allowed"):
        if data.get(key) is not None:
            data[key] = bool(data[key])
    data["license_evidence"] = loads(data.pop("license_evidence_json", None), {}) or {}
    return data


def create_source_asset(payload: dict[str, Any]) -> dict[str, Any]:
    ownership_type = str(payload.get("ownership_type") or "unknown").strip().lower()
    source_type = str(payload.get("source_type") or "").strip().lower()
    title = str(payload.get("title") or "").strip()
    source_url = str(payload.get("source_url") or "").strip() or None
    local_ref = str(payload.get("local_ref") or "").strip() or None
    checksum = str(payload.get("checksum") or "").strip() or None
    if not title:
        raise ValueError("asset title is required")
    if not source_url and not local_ref:
        raise ValueError("source_url or local_ref is required")

    commercial = payload.get("commercial_use_allowed")
    redistribute = payload.get("redistribution_allowed")
    # self_owned is an explicit operator declaration, not an inference from a URL/domain.
    if ownership_type == "self_owned":
        if commercial is None:
            commercial = True
        if redistribute is None:
            redistribute = True

    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO source_assets(
                   source_type,source_url,local_ref,title,author,checksum,ownership_type,license_name,license_url,
                   commercial_use_allowed,redistribution_allowed,license_evidence_json,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source_type,
                source_url,
                local_ref,
                title,
                payload.get("author"),
                checksum,
                ownership_type,
                payload.get("license_name"),
                payload.get("license_url"),
                None if commercial is None else (1 if bool(commercial) else 0),
                None if redistribute is None else (1 if bool(redistribute) else 0),
                dumps(payload.get("license_evidence") or {}),
                "ACTIVE",
                now,
                now,
            ),
        )
        asset_id = int(cur.lastrowid)
    asset = get_source_asset(asset_id)
    if not asset:
        raise RuntimeError("source asset insert failed")
    return asset


def update_source_asset(asset_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_source_asset(asset_id)
    if not current:
        raise KeyError("source asset not found")
    allowed = {
        "title",
        "source_url",
        "local_ref",
        "author",
        "checksum",
        "ownership_type",
        "license_name",
        "license_url",
        "commercial_use_allowed",
        "redistribution_allowed",
        "license_evidence",
    }
    changes = {key: value for key, value in payload.items() if key in allowed and value is not None}
    if not changes:
        return current

    ownership = str(changes.get("ownership_type", current.get("ownership_type") or "unknown")).lower()
    if ownership == "self_owned":
        changes.setdefault("commercial_use_allowed", True)
        changes.setdefault("redistribution_allowed", True)

    columns: list[str] = []
    params: list[Any] = []
    for key, value in changes.items():
        column = "license_evidence_json" if key == "license_evidence" else key
        columns.append(f"{column}=?")
        if key == "license_evidence":
            params.append(dumps(value or {}))
        elif key in {"commercial_use_allowed", "redistribution_allowed"}:
            params.append(1 if bool(value) else 0)
        else:
            params.append(value)
    columns.append("updated_at=?")
    params.append(now_iso())
    params.append(asset_id)
    with conn() as c:
        c.execute(f"UPDATE source_assets SET {','.join(columns)} WHERE id=?", tuple(params))
    updated = get_source_asset(asset_id)
    if not updated:
        raise KeyError("source asset not found")
    return updated


def get_source_asset(asset_id: int) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute("SELECT * FROM source_assets WHERE id=?", (asset_id,)).fetchone()
    return _decode_asset(row)


def list_source_assets(limit: int = 100) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM source_assets WHERE status!='DELETED' ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [_decode_asset(row) or {} for row in rows]


def link_product_asset(product_id: int, asset_id: int, role: str = "deliverable") -> dict[str, Any]:
    role = str(role or "deliverable").strip().lower()
    if role not in {"deliverable", "reference", "source", "preview"}:
        raise ValueError("unsupported product asset role")
    now = now_iso()
    with conn() as c:
        product = c.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            raise KeyError("product not found")
        asset = c.execute("SELECT id FROM source_assets WHERE id=? AND status='ACTIVE'", (asset_id,)).fetchone()
        if not asset:
            raise KeyError("source asset not found")
        c.execute(
            """INSERT OR IGNORE INTO product_assets(product_id,source_asset_id,role,created_at)
               VALUES(?,?,?,?)""",
            (product_id, asset_id, role, now),
        )
        c.execute("UPDATE products SET updated_at=? WHERE id=?", (now, product_id))
    return {"product_id": product_id, "source_asset_id": asset_id, "role": role, "status": "LINKED"}


def unlink_product_asset(product_id: int, asset_id: int, role: str | None = None) -> dict[str, Any]:
    now = now_iso()
    with conn() as c:
        product = c.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            raise KeyError("product not found")
        if role:
            cur = c.execute(
                "DELETE FROM product_assets WHERE product_id=? AND source_asset_id=? AND role=?",
                (product_id, asset_id, role),
            )
        else:
            cur = c.execute(
                "DELETE FROM product_assets WHERE product_id=? AND source_asset_id=?",
                (product_id, asset_id),
            )
        c.execute("UPDATE products SET updated_at=? WHERE id=?", (now, product_id))
    return {"product_id": product_id, "source_asset_id": asset_id, "removed": int(cur.rowcount or 0)}


def list_product_assets(product_id: int) -> list[dict[str, Any]]:
    with conn() as c:
        product = c.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            raise KeyError("product not found")
        rows = c.execute(
            """SELECT pa.role,pa.created_at linked_at,sa.*
               FROM product_assets pa
               JOIN source_assets sa ON sa.id=pa.source_asset_id
               WHERE pa.product_id=? AND sa.status!='DELETED'
               ORDER BY pa.role,sa.id""",
            (product_id,),
        ).fetchall()
    result = []
    for row in rows:
        asset = _decode_asset(row) or {}
        result.append(asset)
    return result


def _asset_license_decision(asset: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    ownership = str(asset.get("ownership_type") or "unknown").lower()
    commercial = asset.get("commercial_use_allowed")
    redistribute = asset.get("redistribution_allowed")
    evidence = asset.get("license_evidence") or {}
    license_url = str(asset.get("license_url") or "").strip()
    license_name = str(asset.get("license_name") or "").strip()

    if commercial is False:
        reasons.append("commercial_use_explicitly_disallowed")
    if redistribute is False:
        reasons.append("redistribution_explicitly_disallowed")
    if reasons:
        return "BLOCKED", reasons

    if commercial is not True:
        reasons.append("commercial_use_not_verified")
    if redistribute is not True:
        reasons.append("redistribution_not_verified")
    if ownership in {"licensed", "open_source", "public_domain"} and not (license_url or evidence):
        reasons.append("license_evidence_missing")
    if ownership in {"licensed", "open_source"} and not license_name:
        reasons.append("license_name_missing")
    if ownership == "unknown":
        reasons.append("ownership_unknown")
    return ("REVIEW_REQUIRED", reasons) if reasons else ("PASS", [])


def product_asset_readiness(product_id: int) -> dict[str, Any]:
    assets = list_product_assets(product_id)
    per_asset: list[dict[str, Any]] = []
    blockers: list[str] = []
    review_flags: list[str] = []
    for asset in assets:
        decision, reasons = _asset_license_decision(asset)
        entry = {
            "asset_id": int(asset["id"]),
            "title": asset.get("title"),
            "role": asset.get("role"),
            "ownership_type": asset.get("ownership_type"),
            "decision": decision,
            "reasons": reasons,
        }
        per_asset.append(entry)
        if decision == "BLOCKED":
            blockers.extend(f"asset:{asset['id']}:{reason}" for reason in reasons)
        elif decision == "REVIEW_REQUIRED":
            review_flags.extend(f"asset:{asset['id']}:{reason}" for reason in reasons)

    if not assets:
        review_flags.append("no_source_assets_bound")
        decision = "REVIEW_REQUIRED"
    elif blockers:
        decision = "BLOCKED"
    elif review_flags:
        decision = "REVIEW_REQUIRED"
    else:
        decision = "PASS"

    snapshot = [
        {
            "id": asset.get("id"),
            "role": asset.get("role"),
            "checksum": asset.get("checksum"),
            "ownership_type": asset.get("ownership_type"),
            "license_name": asset.get("license_name"),
            "license_url": asset.get("license_url"),
            "commercial_use_allowed": asset.get("commercial_use_allowed"),
            "redistribution_allowed": asset.get("redistribution_allowed"),
            "license_evidence": asset.get("license_evidence") or {},
            "updated_at": asset.get("updated_at"),
        }
        for asset in assets
    ]
    return {
        "product_id": product_id,
        "decision": decision,
        "policy_version": LICENSE_POLICY_VERSION,
        "asset_count": len(assets),
        "assets": per_asset,
        "blockers": blockers,
        "review_flags": review_flags,
        "snapshot_hash": stable_hash(LICENSE_POLICY_VERSION, dumps(snapshot)),
        "data_policy": "license decisions use explicit operator/source metadata only; no license is inferred from a URL",
    }
