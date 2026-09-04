from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CollectedItem:
    platform: str
    source_type: str
    title: str
    raw: dict[str, Any]
    source_item_id: str | None = None
    source_url: str | None = None
    content: str | None = None
    author: str | None = None
    published_at: str | None = None
    engagement: dict[str, Any] = field(default_factory=dict)
    content_kind: str = "post"
    parent_source_item_id: str | None = None
    thread_url: str | None = None
    published_time_quality: str | None = None

    def validate(self) -> None:
        if not self.title or not self.title.strip():
            raise ValueError("collector item missing title")
        if not self.source_url and not self.source_item_id:
            raise ValueError("collector item missing both source_url and source_item_id")
        if not isinstance(self.raw, dict) or not self.raw:
            raise ValueError("collector item missing raw evidence payload")


class CollectorError(RuntimeError):
    pass
