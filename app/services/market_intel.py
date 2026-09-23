from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Any

from ..config import BASE_DIR, settings
from ..db import conn, dumps, loads, now_iso, row_to_dict, stable_hash
from ..collectors import collect_etsy_market, collect_xianyu_market


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _log_score(value: float | None, saturation: float) -> float | None:
    if value is None or value < 0:
        return None
    if value == 0:
        return 0.0
    return round(min(100.0, math.log1p(value) / math.log1p(saturation) * 100.0), 2)


def _scores(metrics: dict[str, Any]) -> tuple[float, float | None, float]:
    rank = _num(metrics.get("rank_position"))
    views = _log_score(_num(metrics.get("views_count")), 100000.0)
    favorites = _log_score(_num(metrics.get("favorites_count")), 10000.0)
    wants = _log_score(_num(metrics.get("want_count")), 10000.0)
    reviews = _log_score(_num(metrics.get("review_count")), 3000.0)
    rating = _num(metrics.get("rating"))
    components: list[tuple[float, float]] = []
    if rank and rank > 0:
        components.append((max(0.0, 100.0 - min(99.0, (rank - 1.0) * 2.5)), 0.45))
    if views is not None:
        components.append((views, 0.20))
    if favorites is not None:
        components.append((favorites, 0.15))
    if wants is not None:
        components.append((wants, 0.15))
    if reviews is not None:
        # Review count is real, but a shop-level count is weaker evidence for one listing.
        review_scope = str(metrics.get("review_scope") or "").lower()
        review_factor = {"listing": 1.0, "product": 1.0, "listing_search_card": 1.0, "shop": 0.65, "seller": 0.65}.get(review_scope, 1.0)
        components.append((round(reviews * review_factor, 2), 0.15))
    if rating is not None and 0.0 <= rating <= 5.0:
        # Rating contributes only when explicitly observed; shop ratings are down-weighted.
        rating_scope = str(metrics.get("rating_scope") or "").lower()
        rating_factor = {"listing": 1.0, "product": 1.0, "listing_search_card": 1.0, "shop": 0.65, "seller": 0.65}.get(rating_scope, 1.0)
        components.append((round(rating / 5.0 * 100.0 * rating_factor, 2), 0.05))
    if components:
        weight = sum(item[1] for item in components)
        heat = sum(value * w for value, w in components) / max(weight, 0.0001)
    else:
        heat = 0.0
    if metrics.get("bestseller"):
        heat = max(heat, 70.0)
    if metrics.get("trending"):
        heat = max(heat, 62.0)
    heat = round(min(100.0, heat), 2)

    sales = _num(metrics.get("sales_count"))
    verified = bool(metrics.get("sales_verified")) and sales is not None
    sales_score = _log_score(sales, 10000.0) if verified else None
    if sales_score is not None:
        scope = str(metrics.get("sales_scope") or "").lower()
        factor = {"listing": 1.0, "product": 1.0, "shop": 0.65, "seller": 0.65, "account": 0.50}.get(scope)
        if factor is None:
            sales_score = None
            market = heat
        else:
            sales_score = round(sales_score * factor, 2)
            market = round(heat * 0.55 + sales_score * 0.45, 2)
    else:
        market = heat
    return heat, sales_score, market


@lru_cache(maxsize=1)
def _semantic_aliases() -> dict[str, list[str]]:
    path = BASE_DIR / "config" / "semantic_aliases.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(key): [str(value).lower() for value in values if str(value).strip()]
            for key, values in raw.items()
            if isinstance(values, list)
        }
    except Exception:
        return {}


def _tokens(text: Any) -> set[str]:
    value = str(text or "").lower()
    words = {x for x in re.findall(r"[a-z0-9]{2,}", value)}
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", value)
    grams: set[str] = set()
    for run in cjk_runs:
        grams.update(run[i:i+2] for i in range(max(0, len(run) - 1)))
        if len(run) == 1:
            grams.add(run)
    concepts: set[str] = set()
    for concept, aliases in _semantic_aliases().items():
        for alias in aliases:
            if alias.isascii() and len(alias) <= 3:
                matched = re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", value, re.I)
            else:
                matched = alias in value
            if matched:
                concepts.add("@" + concept)
                break
    return words | grams | concepts


def _similarity(left: Any, right: Any) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    lexical = len(a & b) / len(a | b)
    ca = {token for token in a if token.startswith("@")}
    cb = {token for token in b if token.startswith("@")}
    shared = ca & cb
    if not shared:
        return lexical
    generic = {"@template", "@spreadsheet", "@automation", "@document"}
    strong = shared - generic
    if strong:
        concept_score = len(shared) / max(1, len(ca | cb))
        return max(lexical, 0.34 + concept_score * 0.46)
    if len(shared & generic) >= 2:
        concept_score = len(shared) / max(1, len(ca | cb))
        return max(lexical, 0.22 + concept_score * 0.34)
    return lexical


def sync_market_items(source: dict[str, Any], items: list[Any]) -> dict[str, Any]:
    inserted = updated = rejected = 0
    verified_sales_count = verified_rating_count = verified_review_count = 0
    item_ids: list[int] = []
    observed_at = now_iso()
    with conn() as c:
        for item in items:
            raw = item.raw if isinstance(getattr(item, "raw", None), dict) else {}
            metrics = raw.get("market_metrics") if isinstance(raw.get("market_metrics"), dict) else {}
            source_url = str(getattr(item, "source_url", None) or "").strip()
            title = str(getattr(item, "title", None) or "").strip()
            source_item_id = str(getattr(item, "source_item_id", None) or "").strip() or None
            if not title or (not source_url and not source_item_id):
                rejected += 1
                continue
            if raw.get("digital_product_verified") is not True:
                rejected += 1
                continue
            canonical = stable_hash(getattr(item, "platform", "market"), source_item_id or source_url)
            heat_score, sales_score, market_score = _scores(metrics)
            sales_count = _num(metrics.get("sales_count"))
            if bool(metrics.get("sales_verified")) and sales_count is not None:
                verified_sales_count += 1
            rating_value = _num(metrics.get("rating"))
            if rating_value is not None and 0.0 <= rating_value <= 5.0:
                verified_rating_count += 1
            review_value = _num(metrics.get("review_count"))
            if review_value is not None and review_value >= 0.0:
                verified_review_count += 1
            rank_position = _num(metrics.get("rank_position"))
            values = {
                "source_id": int(source["id"]),
                "marketplace": str(getattr(item, "platform", None) or source.get("platform") or "market"),
                "source_item_id": source_item_id,
                "source_url": source_url or None,
                "title": title,
                "seller": getattr(item, "author", None) or raw.get("seller") or raw.get("shop_name"),
                "category": raw.get("category"),
                "price": _num(metrics.get("price")),
                "currency": metrics.get("currency"),
                "sales_count": sales_count,
                "sales_scope": metrics.get("sales_scope"),
                "sales_verified": 1 if bool(metrics.get("sales_verified")) and sales_count is not None else 0,
                "digital_verified": 1,
                "digital_verification": raw.get("digital_verification"),
                "review_count": _num(metrics.get("review_count")),
                "rating": _num(metrics.get("rating")),
                "favorites_count": _num(metrics.get("favorites_count")),
                "views_count": _num(metrics.get("views_count")),
                "rank_position": int(rank_position) if rank_position is not None else None,
                "bestseller": 1 if metrics.get("bestseller") else 0,
                "trending": 1 if metrics.get("trending") else 0,
                "heat_score": heat_score,
                "sales_score": sales_score,
                "market_score": market_score,
                "metrics_json": dumps(metrics),
                "raw_json": dumps(raw),
                "canonical_key": canonical,
            }
            existing = c.execute("SELECT id FROM market_intel_items WHERE canonical_key=?", (canonical,)).fetchone()
            if existing:
                item_id = int(existing["id"])
                assignments = ",".join(f"{key}=?" for key in values if key != "canonical_key")
                params = [values[key] for key in values if key != "canonical_key"]
                c.execute(
                    f"UPDATE market_intel_items SET {assignments},last_seen_at=?,updated_at=? WHERE id=?",
                    (*params, observed_at, observed_at, item_id),
                )
                updated += 1
            else:
                columns = list(values.keys()) + ["first_seen_at", "last_seen_at", "created_at", "updated_at"]
                placeholders = ",".join("?" for _ in columns)
                cur = c.execute(
                    f"INSERT INTO market_intel_items({','.join(columns)}) VALUES({placeholders})",
                    (*values.values(), observed_at, observed_at, observed_at, observed_at),
                )
                item_id = int(cur.lastrowid)
                inserted += 1
            c.execute(
                """INSERT INTO market_intel_snapshots(
                       market_item_id,observed_at,price,sales_count,sales_scope,sales_verified,review_count,rating,
                       favorites_count,views_count,rank_position,bestseller,trending,heat_score,sales_score,market_score,metrics_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id,
                    observed_at,
                    values["price"],
                    values["sales_count"],
                    values["sales_scope"],
                    values["sales_verified"],
                    values["review_count"],
                    values["rating"],
                    values["favorites_count"],
                    values["views_count"],
                    values["rank_position"],
                    values["bestseller"],
                    values["trending"],
                    heat_score,
                    sales_score,
                    market_score,
                    values["metrics_json"],
                ),
            )
            item_ids.append(item_id)

    matched = rematch_market_intel(item_ids)
    return {
        "fetched": len(items),
        "inserted": inserted,
        "updated": updated,
        "duplicates": max(0, len(items) - inserted - rejected),
        "rejected_invalid": rejected,
        "market_item_ids": item_ids,
        "matched_links": matched,
        "verified_sales": verified_sales_count,
        "verified_ratings": verified_rating_count,
        "verified_review_counts": verified_review_count,
    }


def rematch_market_intel(item_ids: list[int] | None = None) -> int:
    with conn() as c:
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            rows = c.execute(
                f"SELECT id,title,category FROM market_intel_items WHERE digital_verified=1 AND id IN ({placeholders})",
                tuple(item_ids),
            ).fetchall()
            c.execute(
                f"DELETE FROM opportunity_market_intel WHERE market_item_id IN ({placeholders})",
                tuple(item_ids),
            )
        else:
            rows = c.execute("SELECT id,title,category FROM market_intel_items WHERE digital_verified=1").fetchall()
            c.execute("DELETE FROM opportunity_market_intel")
        opportunities = c.execute(
            "SELECT id,title,normalized_need,category FROM opportunities WHERE cluster_active=1"
        ).fetchall()
        created = 0
        for item in rows:
            left = f"{item['title']} {item['category'] or ''}"
            candidates: list[tuple[float, int]] = []
            for opp in opportunities:
                right = f"{opp['title']} {opp['normalized_need'] or ''} {opp['category'] or ''}"
                score = _similarity(left, right)
                if score >= float(settings.market_intel_match_threshold):
                    candidates.append((score, int(opp["id"])))
            candidates.sort(reverse=True)
            for score, opportunity_id in candidates[:8]:
                c.execute(
                    """INSERT OR REPLACE INTO opportunity_market_intel(opportunity_id,market_item_id,similarity,matched_at)
                       VALUES(?,?,?,?)""",
                    (opportunity_id, int(item["id"]), round(score, 4), now_iso()),
                )
                created += 1
        return created


def _decode(row: Any) -> dict[str, Any]:
    data = row_to_dict(row) or {}
    for key in ("sales_verified", "digital_verified", "bestseller", "trending"):
        data[key] = bool(data.get(key))
    data["metrics"] = loads(data.pop("metrics_json", None), {}) or {}
    data.pop("raw_json", None)
    data["high_heat"] = float(data.get("heat_score") or 0) >= float(settings.market_intel_high_heat_score)
    data["high_sales"] = (
        bool(data.get("sales_verified"))
        and data.get("sales_score") is not None
        and float(data.get("sales_score") or 0) >= float(settings.market_intel_high_sales_score)
    )
    data["hot_and_selling"] = data["high_heat"] and data["high_sales"]
    data["metric_availability"] = {
        "rating": data.get("rating") is not None,
        "rating_scope": data.get("metrics", {}).get("rating_scope"),
        "review_count": data.get("review_count") is not None,
        "review_scope": data.get("metrics", {}).get("review_scope"),
        "favorites_count": data.get("favorites_count") is not None,
        "want_count": data.get("metrics", {}).get("want_count") is not None,
        "views_count": data.get("views_count") is not None,
        "sales": bool(data.get("sales_verified")) and data.get("sales_count") is not None,
        "sales_scope": data.get("sales_scope"),
    }
    data["score_basis"] = "heat_plus_sales" if data.get("sales_score") is not None else "heat_only"
    return data


def list_market_intel(
    limit: int = 100,
    *,
    high_heat: bool = False,
    high_sales: bool = False,
    hot_and_selling: bool = False,
) -> list[dict[str, Any]]:
    where: list[str] = ["digital_verified=1"]
    params: list[Any] = []
    if high_heat or hot_and_selling:
        where.append("heat_score>=?")
        params.append(float(settings.market_intel_high_heat_score))
    if high_sales or hot_and_selling:
        where.append("sales_verified=1 AND sales_score>=?")
        params.append(float(settings.market_intel_high_sales_score))
    sql = "SELECT * FROM market_intel_items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY market_score DESC, heat_score DESC, coalesce(sales_score,-1) DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
    return [_decode(row) for row in rows]


def market_intel_for_opportunity(opportunity_id: int, limit: int = 12) -> dict[str, Any]:
    with conn() as c:
        rows = c.execute(
            """SELECT mi.*,omi.similarity
               FROM opportunity_market_intel omi
               JOIN market_intel_items mi ON mi.id=omi.market_item_id
               WHERE omi.opportunity_id=? AND mi.digital_verified=1
               ORDER BY mi.market_score DESC,omi.similarity DESC
               LIMIT ?""",
            (opportunity_id, max(1, min(limit, 50))),
        ).fetchall()
    items = [_decode(row) for row in rows]
    verified_sales = [item for item in items if item.get("sales_verified") and item.get("sales_count") is not None]
    verified_ratings = [item for item in items if item.get("rating") is not None]
    verified_reviews = [item for item in items if item.get("review_count") is not None]
    return {
        "matched_count": len(items),
        "high_heat_count": sum(1 for item in items if item["high_heat"]),
        "high_sales_count": sum(1 for item in items if item["high_sales"]),
        "hot_and_selling_count": sum(1 for item in items if item["hot_and_selling"]),
        "verified_sales_count": len(verified_sales),
        "verified_rating_count": len(verified_ratings),
        "verified_review_count": len(verified_reviews),
        "sales_scopes": sorted({str(item.get("sales_scope")) for item in verified_sales if item.get("sales_scope")}),
        "rating_scopes": sorted({str(item.get("metrics", {}).get("rating_scope")) for item in verified_ratings if item.get("metrics", {}).get("rating_scope")}),
        "review_scopes": sorted({str(item.get("metrics", {}).get("review_scope")) for item in verified_reviews if item.get("metrics", {}).get("review_scope")}),
        "max_heat_score": max((float(item.get("heat_score") or 0) for item in items), default=0.0),
        "max_sales_score": max((float(item.get("sales_score") or 0) for item in verified_sales), default=0.0),
        "items": items,
        "truth_policy": (
            "sales/rating/review metrics are stored only when observed from the source; "
            "their scope is preserved (listing/product/shop/seller/account) and shop metrics are never promoted to listing metrics."
        ),
    }



_MARKET_QUERY_STOPWORDS = {
    "solo", "agency", "agencies", "gtm", "playbook", "checklist", "validator",
    "stack", "guide", "kit", "toolkit", "template", "blueprint", "setup",
    "operations", "operation", "specification", "workflow", "workflows",
}


def targeted_market_queries(
    primary_product: str,
    platform: str = "etsy",
    max_queries: int = 2,
) -> list[str]:
    title = " ".join(str(primary_product or "").split())
    if not title:
        raise ValueError("primary product is required for targeted market validation")

    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+._-]*", title)
    concise_words = [
        word
        for word in words
        if word.lower() not in _MARKET_QUERY_STOPWORDS
        and len(word) > 1
    ]
    concise = " ".join(concise_words[:6]).strip()

    platform = str(platform or "etsy").lower()
    queries: list[str] = []
    if platform == "xianyu":
        lower = title.lower()
        if "whatsapp" in lower:
            queries.extend(["WhatsApp 自动化", "WhatsApp 工作流"])
        elif "n8n" in lower:
            queries.extend(["n8n 自动化", "n8n 工作流"])
        elif any(token in lower for token in ("excel", "spreadsheet", "workbook")):
            queries.extend(["Excel 模板", "Excel 自动化表格"])
        elif any(token in lower for token in ("resume", "cv")):
            queries.extend(["简历模板", "简历电子版模板"])
        elif any(token in lower for token in ("invoice", "billing")):
            queries.extend(["账单自动化", "发票表格模板"])
        else:
            # Xianyu search is predominantly Chinese. A concise English fallback is
            # still preferable to an overly long product title.
            if concise:
                queries.append(concise)
            queries.append(title)
    else:
        lower = title.lower()
        if "whatsapp" in lower:
            queries.extend(["WhatsApp automation", "WhatsApp workflow template"])
        elif "n8n" in lower:
            queries.extend(["n8n automation", "n8n workflow template"])
        elif any(token in lower for token in ("excel", "spreadsheet", "workbook")):
            queries.extend(["Excel spreadsheet template", "Excel automation template"])
        elif any(token in lower for token in ("resume", "cv")):
            queries.extend(["resume template", "CV template"])
        elif any(token in lower for token in ("invoice", "billing")):
            queries.extend(["invoice spreadsheet template", "billing spreadsheet template"])
        else:
            # Marketplaces perform better with concise buyer-facing nouns than an
            # Agent-generated product title. Preserve the full title only as a fallback.
            if concise:
                queries.append(concise)
            queries.append(title)

    unique: list[str] = []
    seen: set[str] = set()
    for query in queries:
        value = " ".join(str(query).split())
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique[:max(1, min(int(max_queries), 3))]


def _target_market_source(
    opportunity_id: int,
    platform: str,
    query: str,
) -> dict[str, Any]:
    source_type = {
        "etsy": "etsy_market",
        "xianyu": "xianyu_market",
    }.get(platform)
    if not source_type:
        raise ValueError(f"unsupported targeted market platform: {platform}")

    name = f"WATCH market validation #{opportunity_id} {platform}"
    config = {
        "role": "market",
        "priority": "P1",
        "business_group": "market_intel",
        "recommended": False,
        "managed_refresh": False,
        "mode": "watch_targeted_market_validation",
        "opportunity_id": opportunity_id,
    }
    now = now_iso()
    with conn() as c:
        row = c.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()
        if row:
            source_id = int(row["id"])
            c.execute(
                """UPDATE sources
                   SET source_type=?,platform=?,query=?,url=NULL,config_json=?,enabled=0,updated_at=?
                   WHERE id=?""",
                (source_type, platform, query, dumps(config), now, source_id),
            )
        else:
            cur = c.execute(
                """INSERT INTO sources(
                       name,source_type,platform,query,url,config_json,enabled,status,created_at,updated_at
                   ) VALUES(?,?,?,?,NULL,?,0,'NOT_CHECKED',?,?)""",
                (name, source_type, platform, query, dumps(config), now, now),
            )
            source_id = int(cur.lastrowid)
        source = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return row_to_dict(source) or {}


def _market_signal_signature(intel: dict[str, Any]) -> tuple[Any, ...]:
    rows = []
    for item in intel.get("items") or []:
        rows.append((
            int(item.get("id") or 0),
            round(float(item.get("similarity") or 0), 4),
            round(float(item.get("heat_score") or 0), 2),
            None if item.get("sales_score") is None else round(float(item.get("sales_score") or 0), 2),
            bool(item.get("sales_verified")),
            item.get("sales_count"),
            item.get("sales_scope"),
            item.get("rating"),
            item.get("metrics", {}).get("rating_scope"),
            item.get("review_count"),
            item.get("metrics", {}).get("review_scope"),
        ))
    return tuple(sorted(rows))


def _targeted_market_exception_status(exc: Exception) -> str:
    text = str(exc).lower()
    if "empty_result" in text or "returned no data" in text or "no matching" in text:
        return "EMPTY"
    return "FAILED"


def _targeted_market_validation_status(results: list[dict[str, Any]]) -> dict[str, Any]:
    platforms: dict[str, list[str]] = {}
    for item in results:
        platforms.setdefault(str(item.get("platform") or "unknown"), []).append(
            str(item.get("status") or "FAILED").upper()
        )

    platform_status: dict[str, str] = {}
    for platform, statuses in platforms.items():
        if "SUCCESS" in statuses:
            platform_status[platform] = "SUCCESS"
        elif "EMPTY" in statuses and "FAILED" in statuses:
            platform_status[platform] = "PARTIAL"
        elif statuses and all(status == "EMPTY" for status in statuses):
            platform_status[platform] = "EMPTY"
        else:
            platform_status[platform] = "FAILED"

    values = list(platform_status.values())
    if values and all(value in {"SUCCESS", "EMPTY"} for value in values):
        overall = "COMPLETE"
    elif values and all(value == "FAILED" for value in values):
        overall = "INCONCLUSIVE"
    else:
        overall = "PARTIAL"
    return {
        "validation_status": overall,
        "platform_status": platform_status,
    }


def collect_targeted_market_intel(
    opportunity_id: int,
    primary_product: str,
    *,
    per_query_limit: int = 12,
) -> dict[str, Any]:
    """Validate one WATCH product against real digital-market supply signals.

    This never creates demand evidence. Etsy/Xianyu results must pass each collector's
    existing digital-product verification before entering market_intel.
    """
    queries_by_platform = {
        "etsy": targeted_market_queries(primary_product, "etsy"),
        "xianyu": targeted_market_queries(primary_product, "xianyu"),
    }
    limit = max(1, min(int(per_query_limit), 20))
    before = market_intel_for_opportunity(opportunity_id, 50)
    before_signature = _market_signal_signature(before)

    results: list[dict[str, Any]] = []
    for platform, collector in (
        ("etsy", collect_etsy_market),
        ("xianyu", collect_xianyu_market),
    ):
        for query in queries_by_platform[platform]:
            source = _target_market_source(opportunity_id, platform, query)
            try:
                items = collector(query, limit=limit)
                stats = sync_market_items(source, items)
                results.append({
                    "platform": platform,
                    "query": query,
                    "status": "SUCCESS" if items else "EMPTY",
                    "fetched": len(items),
                    "inserted": int(stats.get("inserted") or 0),
                    "updated": int(stats.get("updated") or 0),
                    "rejected_invalid": int(stats.get("rejected_invalid") or 0),
                    "matched_links": int(stats.get("matched_links") or 0),
                    "verified_sales": int(stats.get("verified_sales") or 0),
                    "verified_ratings": int(stats.get("verified_ratings") or 0),
                    "verified_review_counts": int(stats.get("verified_review_counts") or 0),
                })
            except Exception as exc:
                status = _targeted_market_exception_status(exc)
                results.append({
                    "platform": platform,
                    "query": query,
                    "status": status,
                    "fetched": 0,
                    **({"error": str(exc)} if status == "FAILED" else {"reason": str(exc)}),
                })

    validation = _targeted_market_validation_status(results)
    after = market_intel_for_opportunity(opportunity_id, 50)
    after_signature = _market_signal_signature(after)
    return {
        "opportunity_id": opportunity_id,
        "primary_product": primary_product,
        "queries": queries_by_platform,
        "results": results,
        **validation,
        "before": before,
        "after": after,
        "market_changed": before_signature != after_signature,
        "new_matched_count": max(
            0,
            int(after.get("matched_count") or 0) - int(before.get("matched_count") or 0),
        ),
        "policy": (
            "real verified digital-market supply only; market intel never counts as demand; "
            "sales/rating/review scopes are preserved and missing results remain unknown rather than low competition"
        ),
    }


def market_intel_summary() -> dict[str, Any]:
    with conn() as c:
        row = c.execute(
            """SELECT SUM(CASE WHEN digital_verified=1 THEN 1 ELSE 0 END) total,
                      SUM(CASE WHEN digital_verified=0 THEN 1 ELSE 0 END) excluded_unverified,
                      SUM(CASE WHEN digital_verified=1 AND heat_score>=? THEN 1 ELSE 0 END) high_heat,
                      SUM(CASE WHEN digital_verified=1 AND sales_verified=1 AND sales_score>=? THEN 1 ELSE 0 END) high_sales,
                      SUM(CASE WHEN digital_verified=1 AND heat_score>=? AND sales_verified=1 AND sales_score>=? THEN 1 ELSE 0 END) hot_and_selling,
                      SUM(CASE WHEN digital_verified=1 AND sales_verified=1 THEN 1 ELSE 0 END) verified_sales,
                      SUM(CASE WHEN digital_verified=1 AND rating IS NOT NULL THEN 1 ELSE 0 END) verified_ratings,
                      MAX(CASE WHEN digital_verified=1 THEN heat_score END) max_heat,
                      MAX(CASE WHEN digital_verified=1 THEN sales_score END) max_sales
               FROM market_intel_items""",
            (
                float(settings.market_intel_high_heat_score),
                float(settings.market_intel_high_sales_score),
                float(settings.market_intel_high_heat_score),
                float(settings.market_intel_high_sales_score),
            ),
        ).fetchone()
    data = row_to_dict(row) or {}
    return {
        **{k: int(data.get(k) or 0) for k in ("total", "excluded_unverified", "high_heat", "high_sales", "hot_and_selling", "verified_sales", "verified_ratings")},
        "max_heat": round(float(data.get("max_heat") or 0), 2),
        "max_sales": round(float(data.get("max_sales") or 0), 2),
        "heat_threshold": float(settings.market_intel_high_heat_score),
        "sales_threshold": float(settings.market_intel_high_sales_score),
    }
