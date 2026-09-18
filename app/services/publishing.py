from __future__ import annotations

import json
import mimetypes
import textwrap
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict
from .product_builder import latest_successful_bundle
from .product_lifecycle import get_product, require_approved_current_product


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
                return int(value)
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
    if publish_price <= 0:
        raise ValueError("Etsy publish price must be greater than zero")
    publish_taxonomy = int(taxonomy_id or settings.etsy_taxonomy_id)
    publish_quantity = int(quantity or settings.etsy_listing_quantity)
    if publish_taxonomy <= 0 or publish_quantity <= 0:
        raise ValueError("Etsy taxonomy_id and quantity must be positive")

    previous = _latest_publish_run(product_id)
    if previous and previous.get("status") == "SUCCESS":
        return previous

    created_at = now_iso()
    external_id = int(previous["external_id"]) if previous and previous.get("external_id") else None
    run_id = None
    with conn() as c:
        cur = c.execute(
            """INSERT INTO publish_runs(
                   product_id,platform,status,stage,external_id,external_url,response_json,error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (product_id, "etsy", "RUNNING", "CREATE_DRAFT", str(external_id) if external_id else None, None, None, None, created_at, created_at),
        )
        run_id = int(cur.lastrowid)

    own_client = client is None
    http = client or httpx.Client(timeout=60.0, follow_redirects=True)
    responses: dict[str, Any] = {}
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
                files={"file": (bundle_path.name, handle, mime)},
            )
        response.raise_for_status()
        responses["upload_file"] = _response_json(response)
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

        final_state = "draft"
        if activate:
            with conn() as c:
                c.execute("UPDATE publish_runs SET stage='ACTIVATE',response_json=?,updated_at=? WHERE id=?", (dumps(responses), now_iso(), run_id))
            response = http.put(
                f"{ETSY_BASE_URL}/shops/{settings.etsy_shop_id}/listings/{external_id}",
                headers=headers,
                data={"state": "active"},
            )
            response.raise_for_status()
            responses["activate"] = _response_json(response)
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
                """UPDATE publish_runs SET status='FAILED',external_id=?,response_json=?,error=?,updated_at=? WHERE id=?""",
                (str(external_id) if external_id else None, dumps(responses), str(exc), now_iso(), run_id),
            )
        raise
    finally:
        if own_client:
            http.close()
