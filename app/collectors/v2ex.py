from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .base import CollectedItem, CollectorError
from ..config import settings


def collect_v2ex(mode: str = "hot", limit: int = 30, node_name: str | None = None) -> list[CollectedItem]:
    if mode not in {"hot", "latest", "node"}:
        raise CollectorError("v2ex mode must be hot, latest or node")
    if mode == "hot":
        endpoint = "https://www.v2ex.com/api/topics/hot.json"
        params = None
    elif mode == "latest":
        endpoint = "https://www.v2ex.com/api/topics/latest.json"
        params = None
    else:
        if not node_name:
            raise CollectorError("v2ex node mode requires config.node_name")
        endpoint = "https://www.v2ex.com/api/topics/show.json"
        params = {"node_name": node_name, "page": 1}
    try:
        with httpx.Client(timeout=settings.request_timeout_seconds, headers={"User-Agent": settings.user_agent}) as client:
            resp = client.get(endpoint, params=params)
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        raise CollectorError(f"V2EX request failed: {exc}") from exc

    if not isinstance(body, list):
        raise CollectorError("V2EX returned an unexpected payload")
    items: list[CollectedItem] = []
    for x in body[:limit]:
        title = x.get("title")
        if not title:
            continue
        created = x.get("created")
        published_at = datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="seconds") if created else None
        item = CollectedItem(
            platform="v2ex",
            source_type=f"v2ex_{mode}",
            source_item_id=str(x.get("id")) if x.get("id") is not None else None,
            source_url=x.get("url"),
            title=str(title),
            content=x.get("content") or x.get("content_rendered"),
            author=(x.get("member") or {}).get("username"),
            published_at=published_at,
            engagement={"replies": x.get("replies")},
            raw=x,
        )
        item.validate()
        items.append(item)
    return items
