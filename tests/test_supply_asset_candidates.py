from __future__ import annotations

from pathlib import Path

import pytest

from app.collectors.base import CollectedItem
from app.config import settings
from app.db import conn, init_db, now_iso
from app.services import sources
from app.services.asset_registry import (
    link_product_asset,
    list_source_assets,
    update_source_asset,
)
from app.services.sources import collect_source, create_source


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    old = settings.db_path
    settings.db_path = str(tmp_path / "supply-assets.db")
    init_db()
    yield
    settings.db_path = old


def _github_source() -> int:
    return create_source(
        {
            "name": "GitHub P2 test",
            "source_type": "github",
            "platform": "github",
            "query": "automation template in:name,description",
            "enabled": True,
            "config": {
                "role": "supply",
                "priority": "P2",
                "business_group": "supply_intel",
                "mode": "repositories",
                "limit": 20,
            },
        }
    )


def _repo(name: str, license_spdx: str | None, license_name: str | None = None) -> CollectedItem:
    repo_id = abs(hash(name)) % 1000000 + 1
    license_info = None
    if license_spdx is not None:
        license_info = {
            "spdx_id": license_spdx,
            "name": license_name or license_spdx,
            "url": f"https://api.github.com/licenses/{license_spdx.lower()}",
        }
    raw = {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "full_name": f"owner/{name}",
        "html_url": f"https://github.com/owner/{name}",
        "owner": {"login": "owner"},
        "license": license_info,
        "updated_at": "2026-09-18T00:00:00Z",
        "pushed_at": "2026-09-18T00:00:00Z",
    }
    return CollectedItem(
        platform="github",
        source_type="github_repositories",
        source_item_id=str(repo_id),
        source_url=raw["html_url"],
        title=raw["full_name"],
        content=f"{name} repository",
        author="owner",
        published_at="2026-01-01T00:00:00Z",
        engagement={"stars": 10},
        raw=raw,
    )


def test_p2_github_collection_creates_license_gated_source_assets(monkeypatch):
    source_id = _github_source()
    items = [
        _repo("permissive", "MIT", "MIT License"),
        _repo("copyleft", "GPL-3.0", "GNU GPL v3"),
        _repo("noncommercial", "CC-BY-NC-4.0", "Creative Commons Attribution NonCommercial 4.0"),
        _repo("unknown", None),
    ]
    monkeypatch.setattr(sources, "_dispatch", lambda source, config, limit: items)

    result = collect_source(source_id, force=True)
    assert result["status"] == "SUCCESS"
    sync = result["source_asset_sync"]
    assert sync["created"] == 4
    assert sync["active"] == 1
    assert sync["review_required"] == 2
    assert sync["blocked"] == 1

    by_title = {item["title"]: item for item in list_source_assets(20)}
    assert by_title["owner/permissive"]["status"] == "ACTIVE"
    assert by_title["owner/permissive"]["commercial_use_allowed"] is True
    assert by_title["owner/permissive"]["redistribution_allowed"] is True
    assert by_title["owner/copyleft"]["status"] == "REVIEW_REQUIRED"
    assert by_title["owner/copyleft"]["redistribution_allowed"] is None
    assert by_title["owner/noncommercial"]["status"] == "BLOCKED"
    assert by_title["owner/noncommercial"]["commercial_use_allowed"] is False
    assert by_title["owner/unknown"]["status"] == "REVIEW_REQUIRED"
    assert by_title["owner/unknown"]["ownership_type"] == "unknown"


def test_p2_github_asset_sync_deduplicates_by_repository_url(monkeypatch):
    source_id = _github_source()
    items = [_repo("permissive", "MIT", "MIT License")]
    monkeypatch.setattr(sources, "_dispatch", lambda source, config, limit: items)

    first = collect_source(source_id, force=True)
    second = collect_source(source_id, force=True)

    assert first["source_asset_sync"]["created"] == 1
    assert second["source_asset_sync"]["created"] == 0
    assert second["source_asset_sync"]["updated"] == 1
    assert len(list_source_assets(20)) == 1


def test_review_required_asset_cannot_bind_until_manual_license_review(monkeypatch):
    source_id = _github_source()
    items = [_repo("copyleft", "GPL-3.0", "GNU GPL v3")]
    monkeypatch.setattr(sources, "_dispatch", lambda source, config, limit: items)
    collect_source(source_id, force=True)
    asset = list_source_assets(20)[0]
    assert asset["status"] == "REVIEW_REQUIRED"

    now = now_iso()
    with conn() as c:
        opp = c.execute(
            "INSERT INTO opportunities(title,candidate_key,normalized_need,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("test", "supply-asset-test", "test", "ANALYZED", now, now),
        )
        opportunity_id = int(opp.lastrowid)
        product = c.execute(
            """INSERT INTO products(
                   opportunity_id,sku,name,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?)""",
            (opportunity_id, "DG-SUPPLY-TEST", "test product", "DRAFT", now, now),
        )
        product_id = int(product.lastrowid)

    with pytest.raises(ValueError, match="must be ACTIVE"):
        link_product_asset(product_id, int(asset["id"]), "source")

    reviewed = update_source_asset(
        int(asset["id"]),
        {
            "commercial_use_allowed": True,
            "redistribution_allowed": True,
            "license_evidence": {
                **asset["license_evidence"],
                "manual_review_approved": True,
                "reviewed_by": "operator",
            },
        },
    )
    assert reviewed["status"] == "ACTIVE"
    linked = link_product_asset(product_id, int(asset["id"]), "source")
    assert linked["status"] == "LINKED"
