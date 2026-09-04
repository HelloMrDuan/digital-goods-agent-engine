from __future__ import annotations

from urllib.parse import urljoin

import httpx

from .base import CollectedItem, CollectorError
from .rss import collect_rss
from ..config import settings


def build_rsshub_url(route_or_url: str) -> str:
    value = (route_or_url or "").strip()
    if not value:
        raise CollectorError("RSSHub route/url is empty")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    base = settings.rsshub_base_url.strip().rstrip("/")
    if not base:
        raise CollectorError("RSSHUB_BASE_URL is not configured")
    return base + "/" + value.lstrip("/")


def collect_rsshub(route_or_url: str, platform: str, limit: int = 30) -> list[CollectedItem]:
    url = build_rsshub_url(route_or_url)
    items = collect_rss(url, platform=platform, limit=limit)
    for item in items:
        item.source_type = "rsshub"
        item.raw["rsshub_feed_url"] = url
        item.raw["rsshub_base_url"] = settings.rsshub_base_url
    return items


def doctor() -> dict[str, object]:
    base = settings.rsshub_base_url.strip().rstrip("/")
    if not base:
        return {"configured": False, "status": "NOT_CONFIGURED", "base_url": None}
    # RSSHub instances differ in health endpoints. Probe the route API first and then root.
    urls = [base + "/api/routes", base + "/"]
    errors: list[str] = []
    for url in urls:
        try:
            with httpx.Client(timeout=min(settings.request_timeout_seconds, 10), follow_redirects=True, headers={"User-Agent": settings.user_agent}) as client:
                resp = client.get(url)
            if 200 <= resp.status_code < 400:
                return {"configured": True, "status": "READY", "base_url": base, "probe_url": url, "http_status": resp.status_code}
            errors.append(f"{url}: HTTP {resp.status_code}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    return {"configured": True, "status": "ERROR", "base_url": base, "error": "; ".join(errors)}
