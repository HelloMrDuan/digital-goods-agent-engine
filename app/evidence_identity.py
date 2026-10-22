from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
import re


def account_identity(platform: str, author: str | None, raw: dict[str, Any]) -> tuple[str | None, str]:
    """Identify a source account, never claim verification of a natural person."""
    if platform == "reddit":
        if author and author.strip().lower() not in {"[deleted]", "[removed]", "automoderator"}:
            return f"reddit:username:{author.strip().lower()}", "username"
        return None, "unknown"
    if platform == "xiaohongshu":
        uid = raw.get("userId") or raw.get("user_id") or raw.get("author_id")
        if uid:
            return f"xiaohongshu:uid:{uid}", "profile_id"
        profile = raw.get("author_url") or raw.get("profileUrl") or raw.get("profile_url")
        try:
            parts = urlsplit(str(profile or ""))
        except ValueError:
            return None, "unknown"
        if parts.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}:
            match = re.fullmatch(r"/user/profile/([A-Za-z0-9]+)/?", parts.path)
            if match:
                return f"xiaohongshu:uid:{match.group(1)}", "profile_id"
    if author and author.strip():
        return f"{platform}:display:{author.strip().lower()}", "display_name"
    return None, "unknown"


def evidence_assessment(row: dict[str, Any]) -> dict[str, Any]:
    from .db import loads
    raw = row.get("raw") or loads(row.get("raw_json"), {}) or {}
    account, basis = account_identity(str(row.get("platform") or ""), row.get("author"), raw)
    content = str(row.get("content") or "").strip()
    body_present = bool(content and content.lower() not in {"[deleted]", "[removed]"})
    if row.get("content_kind", "post") != "comment" and content == str(row.get("title") or "").strip():
        body_present = False
    return {
        "body_present": body_present,
        "account_key": account,
        "account_identity_basis": basis,
        "stable_source_account": basis in {"username", "profile_id"},
        "enrichment": raw.get("_enrichment") or {"detail_status": "NOT_REQUESTED", "comments_status": "NOT_REQUESTED"},
    }
