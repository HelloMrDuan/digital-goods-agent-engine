from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

import httpx

from .base import CollectedItem, CollectorError
from ..config import settings

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = html.unescape(_TAG_RE.sub("", str(value))).strip()
    return text or None


def collect_bilibili(query: str, limit: int = 20) -> list[CollectedItem]:
    """Read-only Bilibili search using the public web search endpoint.

    This is a real network collector. If Bilibili rejects the host/IP (412/403/etc.),
    the collector fails explicitly and stores no evidence.
    """
    endpoint = "https://api.bilibili.com/x/web-interface/search/type"
    params = {
        "search_type": "video",
        "keyword": query,
        "page": 1,
        "page_size": min(max(limit, 1), 50),
    }
    headers = {
        "User-Agent": settings.user_agent,
        "Referer": f"https://search.bilibili.com/all?keyword={quote(query)}",
        "Accept": "application/json,text/plain,*/*",
    }
    try:
        with httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True, headers=headers) as client:
            resp = client.get(endpoint, params=params)
        if resp.status_code in {403, 412, 429}:
            raise CollectorError(f"Bilibili rejected this network/IP: HTTP {resp.status_code}; use Agent-Reach bili-cli/OpenCLI or a permitted proxy")
        resp.raise_for_status()
        body = resp.json()
    except CollectorError:
        raise
    except Exception as exc:
        raise CollectorError(f"Bilibili request failed: {exc}") from exc

    if body.get("code") not in (0, None):
        raise CollectorError(f"Bilibili API error code={body.get('code')} message={body.get('message')}")
    rows = ((body.get("data") or {}).get("result") or [])[:limit]
    out: list[CollectedItem] = []
    for raw in rows:
        bvid = raw.get("bvid")
        aid = raw.get("aid")
        title = _clean(raw.get("title"))
        if not title:
            continue
        url = f"https://www.bilibili.com/video/{bvid}" if bvid else (f"https://www.bilibili.com/video/av{aid}" if aid else None)
        item = CollectedItem(
            platform="bilibili",
            source_type="bilibili_search_api",
            source_item_id=str(bvid or aid) if (bvid or aid) else None,
            source_url=url,
            title=title,
            content=_clean(raw.get("description")),
            author=_clean(raw.get("author")),
            published_at=str(raw.get("pubdate") or "") or None,
            engagement={
                "play": raw.get("play"),
                "danmaku": raw.get("video_review"),
                "favorites": raw.get("favorites"),
                "review": raw.get("review"),
            },
            raw=raw,
        )
        item.validate()
        out.append(item)
    return out
