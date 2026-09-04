from __future__ import annotations

from typing import Any

import httpx

from .base import CollectedItem, CollectorError
from ..config import settings


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": settings.user_agent,
    }
    if settings.github_token:
        h["Authorization"] = f"Bearer {settings.github_token}"
    return h


def collect_github(query: str, limit: int = 30, mode: str = "repositories") -> list[CollectedItem]:
    if mode not in {"repositories", "issues"}:
        raise CollectorError("github mode must be repositories or issues")
    endpoint = f"https://api.github.com/search/{mode}"
    params: dict[str, Any] = {"q": query, "per_page": min(limit, 100)}
    if mode == "repositories":
        params.update({"sort": "updated", "order": "desc"})
    else:
        params.update({"sort": "created", "order": "desc"})
    try:
        with httpx.Client(timeout=settings.request_timeout_seconds, headers=_headers()) as client:
            resp = client.get(endpoint, params=params)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                raise CollectorError("GitHub API rate limit reached; configure GITHUB_TOKEN to increase the limit")
            resp.raise_for_status()
            body = resp.json()
    except CollectorError:
        raise
    except Exception as exc:
        raise CollectorError(f"GitHub request failed: {exc}") from exc

    result: list[CollectedItem] = []
    for x in body.get("items", [])[:limit]:
        if mode == "repositories":
            title = x.get("full_name") or x.get("name")
            content = x.get("description")
            engagement = {
                "stars": x.get("stargazers_count"),
                "forks": x.get("forks_count"),
                "open_issues": x.get("open_issues_count"),
                "watchers": x.get("watchers_count"),
            }
            author = (x.get("owner") or {}).get("login")
            published_at = x.get("created_at")
        else:
            title = x.get("title")
            content = x.get("body")
            engagement = {
                "comments": x.get("comments"),
                "score": x.get("score"),
            }
            author = (x.get("user") or {}).get("login")
            published_at = x.get("created_at")
        if not title:
            continue
        item = CollectedItem(
            platform="github",
            source_type=f"github_{mode}",
            source_item_id=str(x.get("id")) if x.get("id") is not None else None,
            source_url=x.get("html_url"),
            title=str(title),
            content=str(content) if content else None,
            author=str(author) if author else None,
            published_at=published_at,
            engagement=engagement,
            raw=x,
        )
        item.validate()
        result.append(item)
    return result
