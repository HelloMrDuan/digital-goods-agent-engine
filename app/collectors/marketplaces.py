from __future__ import annotations

import html as html_lib
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx

from .base import CollectedItem, CollectorError
from ..config import settings


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {k.lower(): v for k, v in attrs}
        href = values.get("href")
        if href:
            self._href = str(href)
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = " ".join(" ".join(self._text).split())
            self.links.append((self._href, text))
            self._href = None
            self._text = []


def _get(url: str) -> str:
    try:
        with httpx.Client(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "en-US,en;q=0.8"},
            trust_env=False,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except Exception as exc:
        raise CollectorError(f"marketplace request failed: {exc}") from exc


def _creative_market_product_links(page_html: str, base_url: str, limit: int) -> list[tuple[str, str, str | None]]:
    parser = _AnchorParser()
    parser.feed(page_html)
    seen: set[str] = set()
    rows: list[tuple[str, str, str | None]] = []
    pattern = re.compile(r"^/([^/?#]+)/([0-9]+)-([^/?#]+)")
    for href, text in parser.links:
        match = pattern.match(href)
        if not match or not text or text.lower() in {"add to cart", "view"}:
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        seller = match.group(1)
        rows.append((full, text, seller))
        if len(rows) >= limit:
            break
    return rows


def collect_creative_market(url: str, limit: int = 30, ranking: str = "bestseller") -> list[CollectedItem]:
    if not url:
        raise CollectorError("Creative Market source missing url")
    if "/templates-themes/" not in url:
        raise CollectorError("Creative Market market-intel source must stay inside downloadable Templates & Themes")
    page = _get(url)
    links = _creative_market_product_links(page, url, limit)
    if not links:
        raise CollectorError("Creative Market page returned no product links; page shape may have changed")

    result: list[CollectedItem] = []
    for rank, (product_url, title, seller) in enumerate(links, start=1):
        match = re.search(r"/([0-9]+)-", product_url)
        product_id = match.group(1) if match else product_url
        result.append(
            CollectedItem(
                platform="creative_market",
                source_type="creative_market_market_intel",
                source_item_id=str(product_id),
                source_url=product_url,
                title=title,
                content=f"Creative Market {ranking} rank #{rank}",
                author=seller,
                engagement={},
                raw={
                    "digital_product_verified": True,
                    "digital_verification": "creative_market_downloadable_templates_category",
                    "market_metrics": {
                        "rank_position": rank,
                        "bestseller": ranking == "bestseller",
                        "trending": ranking == "trending",
                        "sales_count": None,
                        "sales_scope": None,
                        "sales_verified": False,
                        "metric_source_url": url,
                    },
                    "seller": seller,
                    "ranking": ranking,
                },
            )
        )
    return result


def _json_scripts(page_html: str) -> list[Any]:
    values: list[Any] = []
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", page_html, flags=re.I | re.S):
        raw = html_lib.unescape(match.group(1)).strip()
        if not raw or raw[0] not in "[{":
            continue
        try:
            values.append(json.loads(raw))
        except Exception:
            continue
    return values


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_number(obj: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[^0-9.]+", "", value)
            if cleaned:
                try:
                    return float(cleaned)
                except Exception:
                    pass
    return None


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
    return None


def _etsy_obj_digital_status(obj: dict[str, Any]) -> tuple[bool | None, str | None]:
    candidates = [obj]
    for key in ("listing", "listing_card", "listingCard"):
        child = obj.get(key)
        if isinstance(child, dict):
            candidates.append(child)
    for candidate in candidates:
        for key in ("is_digital", "isDigital", "is_download", "isDownload"):
            if key in candidate:
                value = _bool_value(candidate.get(key))
                if value is not None:
                    return value, f"json:{key}"
        for key in ("type", "listing_type", "listingType", "fulfillment_type", "fulfillmentType"):
            value = str(candidate.get(key) or "").strip().lower().replace("-", "_")
            if value in {"download", "digital", "digital_download", "instant_download"}:
                return True, f"json:{key}={value}"
            if value in {"physical", "physical_product"}:
                return False, f"json:{key}={value}"
    return None, None


def _etsy_search_rows(page_html: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _json_scripts(page_html):
        for obj in _walk(root):
            listing_id = obj.get("listing_id") or obj.get("listingId")
            title = obj.get("title")
            if not listing_id or not isinstance(title, str) or not title.strip():
                continue
            key = str(listing_id)
            if key in seen:
                continue
            seen.add(key)
            shop = obj.get("shop") if isinstance(obj.get("shop"), dict) else {}
            shop_name = obj.get("shop_name") or obj.get("shopName") or shop.get("shop_name") or shop.get("shopName")
            url = obj.get("url") or obj.get("listing_url") or f"https://www.etsy.com/listing/{listing_id}"
            price = obj.get("price")
            amount = None
            currency = None
            if isinstance(price, dict):
                raw_amount = price.get("amount")
                divisor = price.get("divisor") or 100
                if isinstance(raw_amount, (int, float)):
                    amount = float(raw_amount) / float(divisor)
                currency = price.get("currency_code") or price.get("currency")
            elif isinstance(price, (int, float)):
                amount = float(price)
            digital, verification = _etsy_obj_digital_status(obj)
            rows.append({
                "listing_id": key,
                "title": title.strip(),
                "url": str(url),
                "shop_name": str(shop_name).strip() if shop_name else None,
                "price": amount,
                "currency": currency,
                "views": _first_number(obj, "views", "view_count"),
                "favorites": _first_number(obj, "num_favorers", "favorites", "favorite_count"),
                "review_count": _first_number(obj, "review_count", "reviews_count", "num_reviews"),
                "rating": _first_number(obj, "rating", "average_rating"),
                "digital": digital,
                "digital_verification": verification,
                "search_rank": len(rows) + 1,
                "raw": obj,
            })
            if len(rows) >= limit:
                return rows
    return rows


def _etsy_listing_digital_status(page_html: str, listing_id: str | None = None) -> tuple[bool | None, str | None]:
    wanted = str(listing_id or "").strip()
    if wanted:
        for root in _json_scripts(page_html):
            for obj in _walk(root):
                current = obj.get("listing_id") or obj.get("listingId")
                if current is None or str(current) != wanted:
                    continue
                status, reason = _etsy_obj_digital_status(obj)
                if status is not None:
                    return status, f"listing_page_{reason}"

    # Etsy listing pages expose buyer-facing labels such as "Digital download" / "Instant Download".
    visible = re.sub(r"<[^>]+>", " ", page_html)
    visible = " ".join(html_lib.unescape(visible).split()).lower()
    if re.search(r"\b(digital download|instant download|digital item)\b", visible):
        return True, "listing_page_digital_download_label"
    return None, None


def _etsy_shop_sales(page_html: str) -> int | None:
    for root in _json_scripts(page_html):
        for obj in _walk(root):
            value = _first_number(obj, "transaction_sold_count", "sales_count", "sold_count")
            if value is not None and value >= 0:
                return int(value)
    text = re.sub(r"<[^>]+>", " ", page_html)
    text = html_lib.unescape(text)
    match = re.search(r"([0-9][0-9,.\s]*)\s+sales\b", text, flags=re.I)
    if not match:
        return None
    digits = re.sub(r"[^0-9]", "", match.group(1))
    return int(digits) if digits else None


def collect_etsy_market(query: str, limit: int = 30) -> list[CollectedItem]:
    if not query:
        raise CollectorError("Etsy market source missing query")
    search_url = f"https://www.etsy.com/search?q={quote_plus(query)}&explicit=1"
    page = _get(search_url)
    candidate_limit = min(60, max(limit, limit * 2))
    rows = _etsy_search_rows(page, candidate_limit)
    if not rows:
        raise CollectorError("Etsy search returned no parseable listing data; page shape or bot protection may have changed")

    verified_rows: list[dict[str, Any]] = []
    verify_budget = max(1, int(settings.etsy_digital_verify_limit))
    detail_checks = 0
    for row in rows:
        digital = row.get("digital")
        verification = row.get("digital_verification")
        if digital is False:
            continue
        if digital is not True:
            if detail_checks >= verify_budget:
                continue
            detail_checks += 1
            try:
                digital, verification = _etsy_listing_digital_status(_get(str(row["url"])), str(row["listing_id"]))
            except Exception:
                digital, verification = None, None
        if digital is not True:
            continue
        row["digital"] = True
        row["digital_verification"] = verification or "verified_digital"
        verified_rows.append(row)
        if len(verified_rows) >= limit:
            break

    if not verified_rows:
        raise CollectorError("Etsy search returned no listings that could be verified as digital downloads")

    shop_sales: dict[str, tuple[int | None, str]] = {}
    shop_budget = max(0, int(settings.etsy_shop_enrich_limit))
    if settings.etsy_enrich_shop_sales and shop_budget:
        for shop_name in [r["shop_name"] for r in verified_rows if r.get("shop_name")]:
            if shop_name in shop_sales or len(shop_sales) >= shop_budget:
                continue
            shop_url = f"https://www.etsy.com/shop/{quote_plus(str(shop_name))}"
            try:
                shop_sales[str(shop_name)] = (_etsy_shop_sales(_get(shop_url)), shop_url)
            except Exception:
                shop_sales[str(shop_name)] = (None, shop_url)

    result: list[CollectedItem] = []
    for row in verified_rows:
        sales_count = None
        sales_url = None
        if row.get("shop_name") in shop_sales:
            sales_count, sales_url = shop_sales[str(row["shop_name"])]
        rank = int(row.get("search_rank") or 0) or None
        result.append(
            CollectedItem(
                platform="etsy",
                source_type="etsy_market_intel",
                source_item_id=str(row["listing_id"]),
                source_url=str(row["url"]),
                title=str(row["title"]),
                content=f"Etsy verified digital search rank #{rank} for {query}",
                author=row.get("shop_name"),
                engagement={
                    "views": row.get("views"),
                    "favorites": row.get("favorites"),
                    "review_count": row.get("review_count"),
                    "rating": row.get("rating"),
                },
                raw={
                    "digital_product_verified": True,
                    "digital_verification": row.get("digital_verification"),
                    "market_metrics": {
                        "rank_position": rank,
                        "bestseller": False,
                        "trending": False,
                        "views_count": row.get("views"),
                        "favorites_count": row.get("favorites"),
                        "review_count": row.get("review_count"),
                        "rating": row.get("rating"),
                        "price": row.get("price"),
                        "currency": row.get("currency"),
                        "sales_count": sales_count,
                        "sales_scope": "shop" if sales_count is not None else None,
                        "sales_verified": sales_count is not None,
                        "sales_metric_source_url": sales_url,
                        "metric_source_url": search_url,
                    },
                    "shop_name": row.get("shop_name"),
                    "query": query,
                    "listing": row.get("raw"),
                },
            )
        )
    return result
