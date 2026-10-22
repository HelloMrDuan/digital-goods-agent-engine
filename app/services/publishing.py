from __future__ import annotations

import base64
import json
import mimetypes
import math
import textwrap
from pathlib import Path
from typing import Any

import httpx

from ..security import redact_error
from PIL import Image, ImageDraw, ImageFont

from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict
from .product_builder import latest_successful_bundle
from .product_lifecycle import get_product, require_approved_current_product
from ..collectors.agent_reach import doctor as agent_reach_doctor, publish_xianyu_listing
from .xianyu_top import xianyu_top_status


ETSY_BASE_URL = "https://api.etsy.com/v3/application"


def etsy_status() -> dict[str, Any]:
    missing = []
    for name, value in {
        "ETSY_API_KEY": settings.etsy_api_key,
        "ETSY_ACCESS_TOKEN": settings.etsy_access_token,
        "ETSY_SHOP_ID": settings.etsy_shop_id,
        "ETSY_TAXONOMY_ID": settings.etsy_taxonomy_id,
        "ETSY_SHOP_CURRENCY": settings.etsy_shop_currency,
    }.items():
        if not str(value or "").strip():
            missing.append(name)
    return {
        "configured": not missing,
        "status": "NOT_CONFIGURED" if missing else "CONFIGURED_NOT_VERIFIED",
        "missing": missing,
        "shop_id": settings.etsy_shop_id or None,
        "taxonomy_id": settings.etsy_taxonomy_id or None,
        "shop_currency": settings.etsy_shop_currency or None,
        "mode": "REAL_ETSY_API",
        "required_scope": "listings_w",
    }


def _headers() -> dict[str, str]:
    return {
        "x-api-key": settings.etsy_api_key,
        "Authorization": f"Bearer {settings.etsy_access_token}",
        "Accept": "application/json",
    }


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception:
        value = {"raw": response.text[:4000]}
    if isinstance(value, dict):
        return value
    return {"data": value}


def _listing_id(payload: dict[str, Any]) -> int:
    candidates = [payload.get("listing_id")]
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        candidates.append(results[0].get("listing_id"))
    for value in candidates:
        try:
            if value is not None:
                parsed = int(value)
                if parsed > 0:
                    return parsed
        except Exception:
            pass
    raise ValueError("Etsy createDraftListing response did not contain listing_id")


def _ascii_cover_text(product: dict[str, Any]) -> str:
    title = str(product.get("title") or product.get("name") or "Digital Product")
    cleaned = title.encode("ascii", errors="ignore").decode("ascii").strip()
    return cleaned[:90] or f"Digital Product {product.get('sku') or product.get('id')}"


def _cover_image(product: dict[str, Any]) -> Path:
    bundle = latest_successful_bundle(int(product["id"]))
    folder = Path(str(bundle["bundle_path"])).parent
    path = folder / "etsy-listing-cover.png"

    image = Image.new("RGB", (1600, 1200), "white")
    draw = ImageDraw.Draw(image)
    try:
        font_big = ImageFont.truetype("DejaVuSans.ttf", 76)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 42)
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()
    title = _ascii_cover_text(product)
    lines = textwrap.wrap(title, width=30)[:4]
    y = 300
    for line in lines:
        draw.text((120, y), line, fill="black", font=font_big)
        y += 105
    draw.text((120, 860), "DIGITAL DOWNLOAD", fill="black", font=font_small)
    draw.text((120, 940), str(product.get("sku") or ""), fill="black", font=font_small)
    image.save(path, format="PNG", optimize=True)
    return path


def _latest_publish_run(product_id: int) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM publish_runs WHERE product_id=? AND platform='etsy' ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
    if not row:
        return None
    item = row_to_dict(row) or {}
    item["response"] = loads(item.pop("response_json", None), {}) or {}
    return item


def list_publish_runs(product_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM publish_runs WHERE product_id=? ORDER BY id DESC LIMIT ?",
            (product_id, max(1, min(limit, 100))),
        ).fetchall()
    result = []
    for row in rows:
        item = row_to_dict(row) or {}
        item["response"] = loads(item.pop("response_json", None), {}) or {}
        result.append(item)
    return result


def publish_to_etsy(
    product_id: int,
    *,
    price: float | None = None,
    currency: str | None = None,
    taxonomy_id: int | None = None,
    quantity: int | None = None,
    activate: bool = True,
    client: httpx.Client | Any | None = None,
) -> dict[str, Any]:
    product = require_approved_current_product(product_id)
    status = etsy_status()
    if not status["configured"]:
        raise ValueError("Etsy publishing is not configured: missing " + ",".join(status["missing"]))

    bundle = latest_successful_bundle(product_id)
    bundle_path = Path(str(bundle["bundle_path"])).resolve()
    product_currency = str(product.get("price_currency") or settings.default_product_currency).upper()
    publish_currency = str(currency or settings.etsy_shop_currency).upper()
    if publish_currency != str(settings.etsy_shop_currency).upper():
        raise ValueError("publish currency must match ETSY_SHOP_CURRENCY")
    if price is None and product_currency != publish_currency:
        raise ValueError(
            f"product price currency {product_currency} differs from Etsy shop currency {publish_currency}; "
            "provide an explicit publish price in the shop currency"
        )
    publish_price = float(price if price is not None else product.get("price_suggestion"))
    if not math.isfinite(publish_price) or publish_price <= 0:
        raise ValueError("Etsy publish price must be greater than zero")
    publish_taxonomy = int(taxonomy_id or settings.etsy_taxonomy_id)
    publish_quantity = int(quantity or settings.etsy_listing_quantity)
    if publish_taxonomy <= 0 or publish_quantity <= 0:
        raise ValueError("Etsy taxonomy_id and quantity must be positive")

    created_at = now_iso()
    external_id = None
    run_id = None
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        previous = c.execute(
            "SELECT * FROM publish_runs WHERE product_id=? AND platform='etsy' ORDER BY id DESC LIMIT 1", (product_id,)
        ).fetchone()
        if previous and previous["status"] in {"RUNNING", "UNKNOWN"}:
            raise ValueError("Etsy publish is RUNNING or UNKNOWN; reconcile the remote listing before retrying")
        previous_response = loads(previous["response_json"], {}) if previous else {}
        previous_bundle = (previous_response or {}).get("bundle_checksum")
        if previous and previous["status"] == "SUCCESS":
            if not previous_bundle or previous_response.get("approved_gate_snapshot_hash") != product.get("approved_gate_snapshot_hash"):
                raise ValueError("published approval changed or legacy proof is missing; reconcile the existing listing")
            if str(previous_response.get("shop_id") or "") != str(settings.etsy_shop_id):
                raise ValueError("Etsy shop changed; reconcile the listing before publishing under another account")
        if previous_bundle and previous_bundle != bundle["checksum"]:
            raise ValueError("published bundle changed; reconcile the existing listing before publishing a revision")
        if previous and previous["status"] == "SUCCESS" and (previous["stage"] == "ACTIVE" or not activate):
            return dict(previous)
        external_id = int(previous["external_id"]) if previous and previous["external_id"] else None
        cur = c.execute(
            """INSERT INTO publish_runs(
                   product_id,platform,status,stage,external_id,external_url,response_json,error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (product_id, "etsy", "RUNNING", "CREATE_DRAFT", str(external_id) if external_id else None, None, None, None, created_at, created_at),
        )
        run_id = int(cur.lastrowid)

    own_client = client is None
    http = client or httpx.Client(timeout=60.0, follow_redirects=True)
    responses: dict[str, Any] = {
        "bundle_checksum": bundle["checksum"],
        "approved_gate_snapshot_hash": product.get("approved_gate_snapshot_hash"),
        "shop_id": settings.etsy_shop_id,
    }
    try:
        headers = _headers()
        if external_id is None:
            response = http.post(
                f"{ETSY_BASE_URL}/shops/{settings.etsy_shop_id}/listings",
                headers=headers,
                data={
                    "quantity": str(publish_quantity),
                    "title": str(product.get("title") or product.get("name")),
                    "description": str(product.get("description") or ""),
                    "price": f"{publish_price:.2f}",
                    "who_made": settings.etsy_who_made,
                    "when_made": settings.etsy_when_made,
                    "taxonomy_id": str(publish_taxonomy),
                    "type": "download",
                },
            )
            response.raise_for_status()
            responses["create_draft"] = _response_json(response)
            external_id = _listing_id(responses["create_draft"])
            with conn() as c:
                c.execute(
                    "UPDATE publish_runs SET stage='UPLOAD_FILE',external_id=?,response_json=?,updated_at=? WHERE id=?",
                    (str(external_id), dumps(responses), now_iso(), run_id),
                )

        mime = mimetypes.guess_type(bundle_path.name)[0] or "application/zip"
        with bundle_path.open("rb") as handle:
            response = http.post(
                f"{ETSY_BASE_URL}/shops/{settings.etsy_shop_id}/listings/{external_id}/files",
                headers=headers,
                data={"name": bundle_path.name},
                files={"file": (bundle_path.name, handle, mime)},
            )
        response.raise_for_status()
        responses["upload_file"] = _response_json(response)
        if not responses["upload_file"].get("listing_file_id"):
            raise ValueError("Etsy did not confirm a listing_file_id for the uploaded package")
        with conn() as c:
            c.execute("UPDATE publish_runs SET stage='UPLOAD_IMAGE',response_json=?,updated_at=? WHERE id=?", (dumps(responses), now_iso(), run_id))

        cover = _cover_image(product)
        with cover.open("rb") as handle:
            response = http.post(
                f"{ETSY_BASE_URL}/shops/{settings.etsy_shop_id}/listings/{external_id}/images",
                headers=headers,
                files={"image": (cover.name, handle, "image/png")},
            )
        response.raise_for_status()
        responses["upload_image"] = _response_json(response)
        if not responses["upload_image"].get("listing_image_id"):
            raise ValueError("Etsy did not confirm a listing_image_id")

        final_state = "draft"
        if activate:
            with conn() as c:
                c.execute("UPDATE publish_runs SET stage='ACTIVATE',response_json=?,updated_at=? WHERE id=?", (dumps(responses), now_iso(), run_id))
            response = http.patch(
                f"{ETSY_BASE_URL}/shops/{settings.etsy_shop_id}/listings/{external_id}",
                headers=headers,
                data={"state": "active"},
            )
            response.raise_for_status()
            responses["activate"] = _response_json(response)
            if (_listing_id(responses["activate"]) != external_id
                    or responses["activate"].get("state") != "active"):
                raise ValueError("Etsy did not confirm the requested listing is active")
            final_state = "active"

        external_url = f"https://www.etsy.com/listing/{external_id}"
        finished_at = now_iso()
        with conn() as c:
            c.execute(
                """UPDATE publish_runs SET status='SUCCESS',stage=?,external_id=?,external_url=?,response_json=?,error=NULL,updated_at=?
                   WHERE id=?""",
                (final_state.upper(), str(external_id), external_url, dumps(responses), finished_at, run_id),
            )
            c.execute(
                """UPDATE products SET status=?,published_at=?,published_platform='etsy',
                          external_listing_id=?,external_listing_url=?,updated_at=? WHERE id=?""",
                ("PUBLISHED" if activate else "PUBLISH_DRAFT", finished_at, str(external_id), external_url, finished_at, product_id),
            )
            c.execute(
                "UPDATE opportunities SET status=?,updated_at=? WHERE id=?",
                ("PUBLISHED" if activate else "READY_TO_PUBLISH", finished_at, int(product["opportunity_id"])),
            )
        return {
            "run_id": run_id,
            "product_id": product_id,
            "platform": "etsy",
            "status": "SUCCESS",
            "state": final_state,
            "external_id": str(external_id),
            "external_url": external_url,
            "responses": responses,
        }
    except Exception as exc:
        with conn() as c:
            c.execute(
                """UPDATE publish_runs SET status='UNKNOWN',external_id=?,response_json=?,error=?,updated_at=? WHERE id=?""",
                (str(external_id) if external_id else None, dumps(responses), redact_error(exc), now_iso(), run_id),
            )
        raise
    finally:
        if own_client:
            http.close()



def _extract_xianyu_item_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("item_id", "itemId"):
            raw = value.get(key)
            if raw not in (None, "") and str(raw).isdigit() and int(raw) > 0:
                return str(raw)
        for child in value.values():
            found = _extract_xianyu_item_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_xianyu_item_id(child)
            if found:
                return found
    return None


def xianyu_partner_order_status() -> dict[str, Any]:
    return xianyu_top_status()


def xianyu_status(client: httpx.Client | Any | None = None) -> dict[str, Any]:
    missing = []
    if not settings.xianyu_publish_category.strip():
        missing.append("XIANYU_PUBLISH_CATEGORY")
    if settings.xianyu_shop_currency.upper() != "CNY":
        missing.append("XIANYU_SHOP_CURRENCY=CNY")

    bridge = settings.agent_reach_bridge_url.strip().rstrip("/")
    if bridge:
        own = client is None
        http = client or httpx.Client(
            timeout=8.0,
            headers={
                **({"Authorization": f"Bearer {settings.agent_reach_bridge_token}"} if settings.agent_reach_bridge_token else {})
            },
        )
        try:
            response = http.get(bridge + "/xianyu/status")
            response.raise_for_status()
            remote = response.json()
        except Exception as exc:
            remote = {"configured": False, "error": redact_error(exc)}
        finally:
            if own:
                http.close()
        return {
            "configured": bool(not missing and remote.get("configured")),
            "missing": missing,
            "mode": "OPENCLI_AUTHENTICATED_BROWSER",
            "bridge": remote,
            "requires_login": "Chrome must be logged into goofish.com",
            "official_order_api": xianyu_partner_order_status(),
        }

    info = agent_reach_doctor()
    channel = (info.get("channels") or {}).get("xianyu") or {}
    opencli = info.get("opencli") or {}
    connected = bool(channel.get("opencli_connected") or opencli.get("connected"))
    return {
        "configured": bool(not missing and connected),
        "missing": missing,
        "mode": "OPENCLI_AUTHENTICATED_BROWSER_LOCAL",
        "opencli_connected": connected,
        "requires_login": "Chrome must be logged into goofish.com",
        "official_order_api": xianyu_partner_order_status(),
    }


def publish_to_xianyu(
    product_id: int,
    *,
    price: float | None = None,
    category: str | None = None,
    condition: str | None = None,
    client: httpx.Client | Any | None = None,
) -> dict[str, Any]:
    product = require_approved_current_product(product_id)
    bundle = latest_successful_bundle(product_id)
    product_currency = str(product.get("price_currency") or settings.default_product_currency).upper()
    if product_currency != "CNY":
        raise ValueError("Xianyu publishing currently requires a CNY product price")

    publish_price = float(price if price is not None else product.get("price_suggestion"))
    if not math.isfinite(publish_price) or publish_price <= 0:
        raise ValueError("Xianyu publish price must be greater than zero")
    publish_category = str(category or settings.xianyu_publish_category).strip()
    publish_condition = str(condition or settings.xianyu_publish_condition).strip()
    if not publish_category:
        raise ValueError("Xianyu publishing is not configured: missing XIANYU_PUBLISH_CATEGORY")
    if publish_condition not in {"全新", "几乎全新", "轻微使用", "明显使用", "老旧"}:
        raise ValueError("unsupported Xianyu condition")

    cover = _cover_image(product)
    image_b64 = base64.b64encode(cover.read_bytes()).decode("ascii")
    created_at = now_iso()
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        previous = c.execute(
            "SELECT * FROM publish_runs WHERE product_id=? AND platform='xianyu' ORDER BY id DESC LIMIT 1", (product_id,)
        ).fetchone()
        if previous and previous["status"] in {"RUNNING", "UNKNOWN"}:
            raise ValueError("Xianyu publish is RUNNING or UNKNOWN; reconcile the remote listing before retrying")
        previous_response = loads(previous["response_json"], {}) if previous else {}
        previous_bundle = (previous_response or {}).get("bundle_checksum")
        if previous and previous["status"] == "SUCCESS" and (
            not previous_bundle or previous_response.get("approved_gate_snapshot_hash") != product.get("approved_gate_snapshot_hash")
        ):
            raise ValueError("published approval changed or legacy proof is missing; reconcile the existing listing")
        if previous_bundle and previous_bundle != bundle["checksum"]:
            raise ValueError("published bundle changed; reconcile the existing listing before publishing a revision")
        if previous and previous["status"] == "SUCCESS":
            return dict(previous)
        cur = c.execute(
            """INSERT INTO publish_runs(
                   product_id,platform,status,stage,external_id,external_url,response_json,error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (product_id, "xianyu", "RUNNING", "PUBLISH_BROWSER", None, None, None, None, created_at, created_at),
        )
        run_id = int(cur.lastrowid)

    title = str(product.get("title") or product.get("name") or "").strip()
    description = str(product.get("description") or "").strip()
    description = (
        description
        + "\n\n【交付说明】虚拟数字商品，无实物物流；交付内容为系统生成并经 Gate B 审核的原创数字文件。"
    ).strip()
    response_payload: dict[str, Any] = {}
    try:
        bridge = settings.agent_reach_bridge_url.strip().rstrip("/")
        if bridge:
            own = client is None
            http = client or httpx.Client(
                timeout=210.0,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {settings.agent_reach_bridge_token}"} if settings.agent_reach_bridge_token else {}),
                },
            )
            try:
                response = http.post(
                    bridge + "/xianyu/publish",
                    json={
                        "title": title,
                        "description": description,
                        "price": publish_price,
                        "condition": publish_condition,
                        "category": publish_category,
                        "images_base64": [image_b64],
                    },
                )
                response.raise_for_status()
                response_payload = response.json()
            finally:
                if own:
                    http.close()
        else:
            response_payload = {
                "status": "SUCCESS",
                "result": publish_xianyu_listing(
                    title, description, publish_price, publish_condition, publish_category, [str(cover)]
                ),
            }

        if response_payload.get("status") != "SUCCESS":
            raise ValueError("Xianyu publish did not report SUCCESS")
        item_id = _extract_xianyu_item_id(response_payload.get("result"))
        if not item_id:
            raise ValueError("Xianyu publish returned no verifiable item_id; product was not marked published")
        external_url = f"https://www.goofish.com/item?categoryId=0&id={item_id}"
        response_payload["bundle_checksum"] = bundle["checksum"]
        response_payload["approved_gate_snapshot_hash"] = product.get("approved_gate_snapshot_hash")
        finished_at = now_iso()
        with conn() as c:
            c.execute(
                """UPDATE publish_runs SET status='SUCCESS',stage='ACTIVE',external_id=?,external_url=?,
                          response_json=?,error=NULL,updated_at=? WHERE id=?""",
                (item_id, external_url, dumps(response_payload), finished_at, run_id),
            )
            c.execute(
                """UPDATE products SET status='PUBLISHED',published_at=?,published_platform='xianyu',
                          external_listing_id=?,external_listing_url=?,updated_at=? WHERE id=?""",
                (finished_at, item_id, external_url, finished_at, product_id),
            )
            c.execute(
                "UPDATE opportunities SET status='PUBLISHED',updated_at=? WHERE id=?",
                (finished_at, int(product["opportunity_id"])),
            )
        return {
            "run_id": run_id,
            "product_id": product_id,
            "platform": "xianyu",
            "status": "SUCCESS",
            "state": "active",
            "external_id": item_id,
            "external_url": external_url,
            "response": response_payload,
        }
    except Exception as exc:
        with conn() as c:
            c.execute(
                "UPDATE publish_runs SET status='UNKNOWN',response_json=?,error=?,updated_at=? WHERE id=?",
                (dumps(response_payload), redact_error(exc), now_iso(), run_id),
            )
        raise
