from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class SourceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    source_type: str = Field(pattern="^(rss|rsshub|google_news|github|v2ex|agent_reach|bilibili|youtube)$")
    platform: str = Field(min_length=2, max_length=80)
    query: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2000)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    query: str | None = None
    url: str | None = None
    config: dict[str, Any] | None = None


class DeliveryIn(BaseModel):
    product_id: int
    platform: str = Field(default="manual", min_length=1, max_length=80)
    order_ref: str = Field(min_length=1, max_length=200)
    buyer_ref: str | None = Field(default=None, max_length=300)
    delivery_uri: str | None = Field(default=None, max_length=2000)
    delivery_type: str = Field(
        default="manual",
        pattern="^(manual|file_download|cloud_drive_link|extraction_code|license_key|api_delivery|manual_review)$",
    )
    resource: dict[str, Any] | None = None
