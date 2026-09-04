from __future__ import annotations

from urllib.parse import quote_plus

from .rss import collect_rss
from .base import CollectedItem


def collect_google_news(query: str, limit: int = 30, hl: str = "zh-CN", gl: str = "CN", ceid: str = "CN:zh-Hans") -> list[CollectedItem]:
    # Google News RSS returns live public news/search results. No synthetic data is generated here.
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl={quote_plus(hl)}&gl={quote_plus(gl)}&ceid={quote_plus(ceid)}"
    items = collect_rss(url, platform="google_news", limit=limit)
    for item in items:
        item.source_type = "google_news_rss"
        item.raw["search_query"] = query
    return items
