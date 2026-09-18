from __future__ import annotations

import math
import re
from typing import Any

from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict, stable_hash


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
    reviews = _log_score(_num(metrics.get("review_count")), 3000.0)
    rating = _num(metrics.get("rating"))
    components: list[tuple[float, float]] = []
    if rank and rank > 0:
        components.append((max(0.0, 100.0 - min(99.0, (rank - 1.0) * 2.5)), 0.45))
    if views is not None:
        components.append((views, 0.20))
    if favorites is not None:
        components.append((favorites, 0.15))
    if reviews is not None:
        # Review count is a real observed metric and can stand on its own.
        components.append((reviews, 0.15))
    if rating is not None and 0.0 <= rating <= 5.0:
        # Rating contributes only when the marketplace explicitly exposes it.
        components.append((round(rating / 5.0 * 100.0, 2), 0.05))
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


def _tokens(text: Any) -> set[str]:
    value = str(text or "").lower()
    words = {x for x in re.findall(r"[a-z0-9]{2,}", value)}
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", value)
    grams: set[str] = set()
    for run in cjk_runs:
        grams.update(run[i:i+2] for i in range(max(0, len(run) - 1)))
        if len(run) == 1:
            grams.add(run)
    return words | grams


def _similarity(left: Any, right: Any) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sync_market_items(source: dict[str, Any], items: list[Any]) -> dict[str, Any]:
    inserted = updated = rejected = 0
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
            canonical = stable_hash(getattr(item, "platform", "market"), source_item_id or source_url)
            heat_score, sales_score, market_score = _scores(metrics)
            sales_count = _num(metrics.get("sales_count"))
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
    }


def rematch_market_intel(item_ids: list[int] | None = None) -> int:
    with conn() as c:
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            rows = c.execute(
                f"SELECT id,title,category FROM market_intel_items WHERE id IN ({placeholders})",
                tuple(item_ids),
            ).fetchall()
            c.execute(
                f"DELETE FROM opportunity_market_intel WHERE market_item_id IN ({placeholders})",
                tuple(item_ids),
            )
        else:
            rows = c.execute("SELECT id,title,category FROM market_intel_items").fetchall()
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
    for key in ("sales_verified", "bestseller", "trending"):
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
        "review_count": data.get("review_count") is not None,
        "favorites_count": data.get("favorites_count") is not None,
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
    where: list[str] = []
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
               WHERE omi.opportunity_id=?
               ORDER BY mi.market_score DESC,omi.similarity DESC
               LIMIT ?""",
            (opportunity_id, max(1, min(limit, 50))),
        ).fetchall()
    items = [_decode(row) for row in rows]
    verified_sales = [item for item in items if item.get("sales_verified") and item.get("sales_count") is not None]
    return {
        "matched_count": len(items),
        "high_heat_count": sum(1 for item in items if item["high_heat"]),
        "high_sales_count": sum(1 for item in items if item["high_sales"]),
        "hot_and_selling_count": sum(1 for item in items if item["hot_and_selling"]),
        "max_heat_score": max((float(item.get("heat_score") or 0) for item in items), default=0.0),
        "max_sales_score": max((float(item.get("sales_score") or 0) for item in verified_sales), default=0.0),
        "items": items,
        "truth_policy": "sales_count is stored only when the source explicitly exposes a sales metric; sales_scope identifies listing/shop/account scope and is never silently promoted to product sales.",
    }


def market_intel_summary() -> dict[str, Any]:
    with conn() as c:
        row = c.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN heat_score>=? THEN 1 ELSE 0 END) high_heat,
                      SUM(CASE WHEN sales_verified=1 AND sales_score>=? THEN 1 ELSE 0 END) high_sales,
                      SUM(CASE WHEN heat_score>=? AND sales_verified=1 AND sales_score>=? THEN 1 ELSE 0 END) hot_and_selling,
                      SUM(CASE WHEN sales_verified=1 THEN 1 ELSE 0 END) verified_sales,
                      SUM(CASE WHEN rating IS NOT NULL THEN 1 ELSE 0 END) verified_ratings,
                      MAX(heat_score) max_heat,
                      MAX(sales_score) max_sales
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
        **{k: int(data.get(k) or 0) for k in ("total", "high_heat", "high_sales", "hot_and_selling", "verified_sales", "verified_ratings")},
        "max_heat": round(float(data.get("max_heat") or 0), 2),
        "max_sales": round(float(data.get("max_sales") or 0), 2),
        "heat_threshold": float(settings.market_intel_high_heat_score),
        "sales_threshold": float(settings.market_intel_high_sales_score),
    }
