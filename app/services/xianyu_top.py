from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..security import redact_error

from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict
from .product_builder import latest_successful_bundle
from .product_lifecycle import require_approved_current_product


ORDER_QUERY_METHOD = "alibaba.idle.isv.order.query"
VIRTUAL_DELIVERY_METHOD = "alibaba.idle.isv.goosefish.virtual.delivery"

_ORDER_STATUS_NAMES = {
    0: "UNKNOWN",
    1: "CREATED_UNPAID",
    2: "PAID",
    3: "SHIPPED",
    4: "SUCCESS",
    5: "REFUNDED",
    6: "CLOSED",
}


def xianyu_top_status() -> dict[str, Any]:
    values = {
        "XIANYU_TOP_APP_KEY": settings.xianyu_top_app_key,
        "XIANYU_TOP_APP_SECRET": settings.xianyu_top_app_secret,
        "XIANYU_TOP_ACCESS_TOKEN": settings.xianyu_top_access_token,
        "XIANYU_TOP_BIZ_CODE": settings.xianyu_top_biz_code,
        "XIANYU_TOP_GATEWAY": settings.xianyu_top_gateway,
    }
    missing = [name for name, value in values.items() if not str(value or "").strip()]
    return {
        "configured": not missing,
        "status": "NOT_CONFIGURED" if missing else "CONFIGURED_NOT_VERIFIED",
        "virtual_delivery_configured": not missing and bool(settings.xianyu_top_seller_id.strip()),
        "virtual_delivery_missing": missing + ([] if settings.xianyu_top_seller_id.strip() else ["XIANYU_TOP_SELLER_ID"]),
        "missing": missing,
        "mode": "OFFICIAL_TOP_PARTNER_API",
        "gateway": settings.xianyu_top_gateway,
        "deployment_requirement": "JuShiTa",
        "methods": {
            "order_query": ORDER_QUERY_METHOD,
            "virtual_delivery": VIRTUAL_DELIVERY_METHOD,
        },
        "note": "Requires Xianyu partner invitation, approved TOP permissions and JuShiTa deployment.",
    }


def _require_top_config() -> None:
    status = xianyu_top_status()
    if not status["configured"]:
        raise ValueError("Xianyu TOP is not configured: missing " + ",".join(status["missing"]))


def _order_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        raise ValueError("Xianyu biz_order_id must be numeric")
    return text


def _top_sign(params: dict[str, Any], secret: str) -> str:
    """TOP MD5 signature: SECRET + sorted key/value pairs + SECRET."""
    if not secret:
        raise ValueError("TOP app secret is empty")
    pieces: list[str] = []
    for key in sorted(params):
        if key == "sign":
            continue
        value = params[key]
        if value is None:
            continue
        pieces.append(str(key))
        pieces.append(str(value))
    raw = secret + "".join(pieces) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _top_common(method: str) -> dict[str, str]:
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "method": method,
        "app_key": settings.xianyu_top_app_key,
        "session": settings.xianyu_top_access_token,
        "timestamp": timestamp,
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
    }


def _top_call(
    method: str,
    biz_params: dict[str, Any],
    *,
    client: httpx.Client | Any | None = None,
) -> dict[str, Any]:
    _require_top_config()
    params: dict[str, Any] = {**_top_common(method), **biz_params}
    params["sign"] = _top_sign(params, settings.xianyu_top_app_secret)

    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.post(settings.xianyu_top_gateway, data=params)
        response.raise_for_status()
        body = response.json()
    finally:
        if own:
            http.close()

    if not isinstance(body, dict):
        raise ValueError("Xianyu TOP returned a non-object response")
    error = body.get("error_response")
    if isinstance(error, dict):
        code = error.get("code") or error.get("sub_code") or "TOP_ERROR"
        message = error.get("sub_msg") or error.get("msg") or "unknown TOP error"
        raise ValueError(f"Xianyu TOP error {code}: {redact_error(message)}")

    roots = [value for key, value in body.items() if key.endswith("_response") and isinstance(value, dict)]
    if roots:
        return roots[0]
    return body


def _find_order(value: Any, requested_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if "order_status" in value:
            current = str(value.get("biz_order_id") or "")
            if current == requested_id:
                return value
        for child in value.values():
            found = _find_order(child, requested_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_order(child, requested_id)
            if found:
                return found
    return None


def _safe_order(value: dict[str, Any], requested_id: str) -> dict[str, Any]:
    try:
        status = int(value.get("order_status"))
    except Exception as exc:
        raise ValueError("Xianyu order response missing valid order_status") from exc
    return {
        "biz_order_id": str(value.get("biz_order_id") or requested_id),
        "order_status": status,
        "order_status_name": _ORDER_STATUS_NAMES.get(status, "UNKNOWN"),
        "order_sub_status": value.get("order_sub_status"),
        "buy_amount": value.get("buy_amount"),
        "payment_fen": value.get("payment"),
        "buyer_nick": value.get("buyer_nick"),
        "seller_nick": value.get("seller_nick"),
        "create_time": value.get("create_time"),
        "pay_time": value.get("pay_time"),
        "ship_time": value.get("ship_time"),
        "end_time": value.get("end_time"),
        "x_global_biz_code": value.get("x_global_biz_code"),
        "item_id": str((value.get("item") or {}).get("item_id") or ""),
        "encryption_seller_id": str(value.get("encryption_seller_id") or ""),
    }


def xianyu_order_query(
    biz_order_id: str,
    *,
    client: httpx.Client | Any | None = None,
) -> dict[str, Any]:
    oid = _order_id(biz_order_id)
    root = _top_call(ORDER_QUERY_METHOD, {
        "param_appraise_isv_order_query": json.dumps({"biz_order_id": int(oid)}, separators=(",", ":"))
    }, client=client)
    raw_order = _find_order(root, oid)
    if not raw_order:
        raise ValueError("Xianyu TOP order query returned no matching order")
    order = _safe_order(raw_order, oid)
    configured_biz = str(settings.xianyu_top_biz_code or "").strip()
    actual_biz = str(order.get("x_global_biz_code") or "").strip()
    if configured_biz and actual_biz and configured_biz != actual_biz:
        raise ValueError(
            f"Xianyu order biz code mismatch: expected {configured_biz}, got {actual_biz}"
        )
    return order


def _virtual_delivery_success(value: Any) -> bool:
    if isinstance(value, dict):
        result = value.get("result")
        if isinstance(result, dict) and result.get("data") is True:
            return True
        for child in value.values():
            if _virtual_delivery_success(child):
                return True
    elif isinstance(value, list):
        return any(_virtual_delivery_success(child) for child in value)
    return False


def _existing_delivery(biz_order_id: str) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM deliveries WHERE platform='xianyu' AND order_ref=? ORDER BY id DESC LIMIT 1",
            (biz_order_id,),
        ).fetchone()
    if not row:
        return None
    item = row_to_dict(row) or {}
    item["resource"] = loads(item.pop("resource_json", None), {}) or {}
    return item


def xianyu_virtual_delivery(
    product_id: int,
    biz_order_id: str,
    *,
    client: httpx.Client | Any | None = None,
) -> dict[str, Any]:
    product = require_approved_current_product(product_id)
    bundle = latest_successful_bundle(product_id)
    oid = _order_id(biz_order_id)

    existing = _existing_delivery(oid)
    if existing and int(existing["product_id"]) != product_id:
        raise ValueError("order is already bound to a different product")
    if existing and existing.get("status") == "VIRTUAL_SHIPPED":
        return {
            **existing,
            "idempotent": True,
            "shipment_confirmed": True,
            "file_transferred": bool((existing.get("resource") or {}).get("file_transferred")),
        }

    order = xianyu_order_query(oid, client=client)
    if int(order["order_status"]) != 2:
        raise ValueError(
            f"Xianyu virtual delivery requires a PAID order (status=2); current status="
            f"{order['order_status']} {order['order_status_name']}"
        )

    # A paid order is insufficient: verify platform listing and stable seller identity.
    listing_id = str(product.get("external_listing_id") or "")
    if product.get("published_platform") != "xianyu" or not listing_id or order.get("item_id") != listing_id:
        raise ValueError("Xianyu order item does not match the product's published Xianyu listing")
    seller_id = settings.xianyu_top_seller_id.strip()
    if not seller_id or order.get("encryption_seller_id") != seller_id:
        raise ValueError("Xianyu order seller is missing or does not match XIANYU_TOP_SELLER_ID")

    # Claim under a database write lock before crossing the remote write boundary.
    # RUNNING/UNKNOWN claims survive process restarts and require reconciliation.
    claimed_at = now_iso()
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        previous = c.execute("SELECT * FROM deliveries WHERE platform='xianyu' AND order_ref=?", (oid,)).fetchone()
        if previous:
            if int(previous["product_id"]) != product_id:
                raise ValueError("order is already bound to a different product")
            if previous["status"] == "VIRTUAL_SHIPPED":
                return {**dict(previous), "idempotent": True, "shipment_confirmed": True, "file_transferred": False}
            if previous["status"] != "FAILED" or previous["delivery_type"] != "xianyu_virtual_top":
                raise ValueError("order delivery is RUNNING or UNKNOWN; reconcile before retrying")
            delivery_id = int(previous["id"])
            c.execute("UPDATE deliveries SET status='RUNNING',updated_at=? WHERE id=?", (claimed_at, delivery_id))
        else:
            cur = c.execute(
                """INSERT INTO deliveries(product_id,platform,order_ref,delivery_type,status,created_at,updated_at)
                   VALUES(?,'xianyu',?,'xianyu_virtual_top','RUNNING',?,?)""",
                (product_id, oid, claimed_at, claimed_at),
            )
            delivery_id = int(cur.lastrowid)

    try:
        root = _top_call(
            VIRTUAL_DELIVERY_METHOD,
            {"idle_open_isv_virtual_delivery_param": json.dumps({"biz_order_id": int(oid)}, separators=(",", ":"))},
            client=client,
        )
        if not _virtual_delivery_success(root):
            raise ValueError("Xianyu TOP virtual delivery did not confirm result.data=true")
    except Exception as exc:
        with conn() as c:
            c.execute("UPDATE deliveries SET status='UNKNOWN',error_message=?,updated_at=? WHERE id=?",
                      (redact_error(exc), now_iso(), delivery_id))
        raise

    shipped_at = now_iso()
    resource = {
        "shipment_confirmed": True,
        "file_transferred": False,
        "delivery_semantics": (
            "Official Xianyu no-logistics shipment confirmation only; "
            "this API does not transfer the generated ZIP to the buyer."
        ),
        "bundle_checksum": bundle.get("checksum"),
        "published_listing_id": product.get("external_listing_id"),
        "top_method": VIRTUAL_DELIVERY_METHOD,
        "order_status_before_delivery": order["order_status"],
    }
    with conn() as c:
        c.execute(
            """UPDATE deliveries SET buyer_ref=?,resource_json=?,error_message=NULL,
                      delivered_at=?,status='VIRTUAL_SHIPPED',updated_at=? WHERE id=? AND status='RUNNING'""",
            (order.get("buyer_nick"), dumps(resource), shipped_at, shipped_at, delivery_id),
        )

    return {
        "id": delivery_id,
        "product_id": product_id,
        "platform": "xianyu",
        "order_ref": oid,
        "status": "VIRTUAL_SHIPPED",
        "shipment_confirmed": True,
        "file_transferred": False,
        "order": order,
        "resource": resource,
        "delivered_at": shipped_at,
    }
