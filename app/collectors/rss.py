from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from .base import CollectedItem, CollectorError
from ..config import settings


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    value = "".join(node.itertext()).strip()
    return value or None


def _child(node: ET.Element, *names: str) -> ET.Element | None:
    wanted = {x.lower() for x in names}
    for c in list(node):
        if _local(c.tag) in wanted:
            return c
    return None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass
    try:
        v = value.replace("Z", "+00:00")
        return datetime.fromisoformat(v).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return value


def _raw_node(node: ET.Element) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in list(node):
        key = _local(c.tag)
        value = _text(c)
        if value:
            if key in out:
                old = out[key]
                out[key] = old + [value] if isinstance(old, list) else [old, value]
            else:
                out[key] = value
        if c.attrib:
            out[f"{key}_attrs"] = dict(c.attrib)
    return out


def collect_rss(url: str, platform: str, limit: int = 30) -> list[CollectedItem]:
    try:
        with httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True, headers={"User-Agent": settings.user_agent}) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except Exception as exc:
        raise CollectorError(f"RSS request failed: {exc}") from exc

    try:
        root = ET.fromstring(resp.content)
    except Exception as exc:
        raise CollectorError(f"RSS/Atom XML parse failed: {exc}") from exc

    root_name = _local(root.tag)
    if root_name == "rss":
        channel = _child(root, "channel")
        nodes = [c for c in list(channel or root) if _local(c.tag) == "item"]
        mode = "rss"
    elif root_name == "feed":
        nodes = [c for c in list(root) if _local(c.tag) == "entry"]
        mode = "atom"
    else:
        # Some feeds use RDF as the root with item children.
        nodes = [c for c in root.iter() if _local(c.tag) in {"item", "entry"}]
        mode = "generic"

    items: list[CollectedItem] = []
    for e in nodes[:limit]:
        title = _text(_child(e, "title"))
        link_node = _child(e, "link")
        link = None
        if link_node is not None:
            link = link_node.attrib.get("href") or _text(link_node)
        item_id = _text(_child(e, "guid", "id")) or link
        content = _text(_child(e, "summary", "description", "content", "encoded"))
        author = _text(_child(e, "author", "creator"))
        published = _text(_child(e, "published", "updated", "pubdate", "date"))
        if not title or not (link or item_id):
            continue
        raw = _raw_node(e)
        raw["feed_mode"] = mode
        item = CollectedItem(
            platform=platform,
            source_type="rss",
            source_item_id=str(item_id) if item_id else None,
            source_url=str(link) if link else None,
            title=title,
            content=content,
            author=author,
            published_at=_parse_date(published),
            engagement={},
            raw=raw,
        )
        item.validate()
        items.append(item)
    return items
