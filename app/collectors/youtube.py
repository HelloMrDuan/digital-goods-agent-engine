from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from .base import CollectedItem, CollectorError


def collect_youtube(query: str, limit: int = 20) -> list[CollectedItem]:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise CollectorError("yt-dlp is not installed; install Agent-Reach or yt-dlp")
    target = f"ytsearch{min(max(limit,1),50)}:{query}"
    p = subprocess.run(
        [exe, "--dump-json", "--skip-download", target],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if p.returncode != 0:
        raise CollectorError((p.stderr or p.stdout or "yt-dlp search failed").strip())
    out: list[CollectedItem] = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw: dict[str, Any] = json.loads(line)
        except Exception:
            continue
        vid = raw.get("id")
        title = raw.get("title")
        if not title or not vid:
            continue
        item = CollectedItem(
            platform="youtube",
            source_type="youtube_yt_dlp",
            source_item_id=str(vid),
            source_url=raw.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
            title=str(title),
            content=raw.get("description"),
            author=raw.get("channel") or raw.get("uploader"),
            published_at=raw.get("upload_date") or raw.get("timestamp"),
            engagement={
                "views": raw.get("view_count"),
                "likes": raw.get("like_count"),
                "comments": raw.get("comment_count"),
            },
            raw=raw,
        )
        item.validate()
        out.append(item)
    if not out:
        raise CollectorError("YouTube search returned no parseable real items")
    return out[:limit]
