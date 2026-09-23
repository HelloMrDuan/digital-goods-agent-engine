from __future__ import annotations

import json
import re
from typing import Any

from .agent_reach import collect as collect_agent_reach
from .base import CollectedItem, CollectorError
from ..config import settings


_DIGITAL_MARKERS = (
    "虚拟商品", "数字商品", "电子版", "电子文件", "数字下载", "下载链接",
    "无需物流", "无实物", "自动发货", "自动秒发", "在线交付", "网盘交付", "网盘发货", "邮箱发送",
    "源码交付", "脚本交付", "表格模板", "excel模板", "excel 模板",
)


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(value: Any, *keys: str) -> Any:
    for obj in _walk(value):
        for key in keys:
            if key in obj and obj[key] not in (None, "", [], {}):
                return obj[key]
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group(0)) if m else None


def _text_blob(item: CollectedItem) -> str:
    raw = ""
    try:
        raw = json.dumps(item.raw, ensure_ascii=False)
    except Exception:
        raw = str(item.raw)
    return " ".join([str(item.title or ""), str(item.content or ""), raw]).lower()


def _digital_verification(item: CollectedItem) -> str | None:
    if item.raw.get("_xianyu_detail_verified") is not True:
        return None
    detail = item.raw.get("_xianyu_detail")
    if not isinstance(detail, dict) or not detail:
        return None
    try:
        text = json.dumps(detail, ensure_ascii=False).lower()
    except Exception:
        text = str(detail).lower()
    for marker in _DIGITAL_MARKERS:
        if marker.lower() in text:
            return f"xianyu_detail_marker:{marker}"
    return None


def collect_xianyu_market(query: str, limit: int = 30) -> list[CollectedItem]:
    if not query:
        raise CollectorError("Xianyu market source missing query")
    requested = max(1, min(int(limit), 50))
    # Search candidates through the authenticated OpenCLI browser adapter and fetch real item details.
    candidates = collect_agent_reach(
        "xianyu",
        query,
        limit=min(50, max(requested * 2, requested)),
        enrich=True,
        detail_limit=min(
            max(1, int(settings.xianyu_market_detail_limit)),
            max(requested * 2, requested),
        ),
        comments_limit=0,
    )
    result: list[CollectedItem] = []
    for search_rank, item in enumerate(candidates, start=1):
        verification = _digital_verification(item)
        if not verification:
            continue
        price = _number(_first(item.raw, "price", "current_price", "currentPrice", "sold_price", "amount"))
        want_count = _number(_first(item.raw, "want_count", "wantCount", "wanted_count", "want_num", "wantNum", "favor_count"))
        view_count = _number(_first(item.raw, "view_count", "viewCount", "views", "browse_count", "browseCount"))
        collect_count = _number(_first(item.raw, "collect_count", "collectCount", "favorite_count", "favorites_count"))
        seller = item.author or _first(item.raw, "seller_name", "sellerName", "user_name", "nick", "nickname")
        category = _first(item.raw, "category", "category_name", "categoryName")
        raw = dict(item.raw)
        raw.update(
            {
                "digital_product_verified": True,
                "digital_verification": verification,
                "seller": str(seller) if seller else None,
                "category": str(category) if category else None,
                "market_metrics": {
                    "rank_position": search_rank,
                    "bestseller": False,
                    "trending": False,
                    "price": price,
                    "currency": "CNY" if price is not None else None,
                    "want_count": want_count,
                    "views_count": view_count,
                    "favorites_count": collect_count,
                    "review_count": None,
                    "rating": None,
                    "sales_count": None,
                    "sales_scope": None,
                    "sales_verified": False,
                    "metric_source_url": item.source_url,
                    "rank_scope": f"xianyu_search:{query}",
                },
            }
        )
        result.append(
            CollectedItem(
                platform="xianyu",
                source_type="xianyu_market_intel",
                source_item_id=item.source_item_id,
                source_url=item.source_url,
                title=item.title,
                content=item.content or f"闲鱼数字商品搜索排名 #{search_rank} · {query}",
                author=str(seller) if seller else None,
                published_at=item.published_at,
                engagement={**(item.engagement or {}), **({"wants": want_count} if want_count is not None else {})},
                raw=raw,
            )
        )
        if len(result) >= requested:
            break
    if not result:
        raise CollectorError("Xianyu returned no listings that could be verified as digital goods")
    return result
