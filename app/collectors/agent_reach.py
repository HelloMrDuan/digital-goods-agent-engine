from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote, urlsplit

import yaml
import httpx

from .base import CollectedItem, CollectorError
from ..config import settings
from ..security import redact_error


def _url_item_id(platform: str, url: str | None) -> str | None:
    parts = urlsplit(str(url or ""))
    if platform == "xiaohongshu" and parts.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        match = re.match(r"/(?:explore|search_result)/([A-Za-z0-9]+)", parts.path)
    elif platform == "reddit" and parts.hostname in {"reddit.com", "www.reddit.com", "old.reddit.com"}:
        match = re.search(r"/comments/([A-Za-z0-9]+)", parts.path)
    else:
        match = None
    return match.group(1) if match else None


def _decode_cli_output(value: bytes | str | None) -> str:
    """Decode CLI output deterministically on Windows.

    Node/OpenCLI and Agent-Reach emit UTF-8, while subprocess(text=True) on
    Simplified-Chinese Windows may default to GBK and crash in the reader
    thread before we can inspect the result. Capture bytes and decode here.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback for native Windows tools that may still emit the local codepage.
        try:
            return value.decode("gb18030")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")


def _resolve_cli_command(cmd: list[str]) -> list[str]:
    """Resolve npm/pip console shims before CreateProcess on Windows.

    `opencli doctor` already used shutil.which() before execution, but the real
    search/read path historically passed a bare `opencli` token. On Windows,
    npm exposes OpenCLI as `opencli.cmd`; CreateProcess may not resolve that
    shim from a bare token even though `shutil.which("opencli")` can. Resolve
    every CLI invocation consistently so doctor and real collection use the
    same executable path.
    """
    if not cmd:
        raise CollectorError("empty CLI command")
    resolved = shutil.which(cmd[0])
    if resolved:
        if resolved.lower().endswith((".cmd", ".bat")):
            # cmd /c interprets & in signed URLs as another command. Resolve npm's
            # Node entry point instead; URLs and queries remain literal argv bytes.
            shim = Path(resolved)
            try:
                source = shim.read_text(encoding="utf-8")
            except OSError as exc:
                raise CollectorError("Cannot read Windows CLI shim") from exc
            match = re.search(r'"%dp0%[\\/]([^"\r\n]+\.(?:mjs|cjs|js))"', source, re.I)
            if match:
                entry = (shim.parent / match.group(1).replace("\\", "/")).resolve()
                local_node = shim.parent / "node.exe"
                node = str(local_node) if local_node.is_file() else shutil.which("node")
                if node and entry.is_file() and entry.is_relative_to(shim.parent.resolve()):
                    return [node, str(entry), *cmd[1:]]
            raise CollectorError("Cannot resolve Windows CLI shim to a direct Node entry point")
        return [resolved, *cmd[1:]]
    return list(cmd)


def _run_cli_text(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    resolved_cmd = _resolve_cli_command(cmd)
    p = subprocess.run(resolved_cmd, capture_output=True, timeout=timeout, check=False)
    return p.returncode, _decode_cli_output(p.stdout), _decode_cli_output(p.stderr)


def _bridge_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.agent_reach_bridge_token:
        h["Authorization"] = f"Bearer {settings.agent_reach_bridge_token}"
    return h


def _bridge_doctor() -> dict[str, Any]:
    base = settings.agent_reach_bridge_url.strip().rstrip("/")
    if not base:
        raise CollectorError("Agent-Reach bridge URL is empty")
    try:
        with httpx.Client(timeout=20, headers=_bridge_headers()) as client:
            resp = client.get(base + "/doctor")
            resp.raise_for_status()
            body = resp.json()
        body["via_bridge"] = True
        body["bridge_url"] = base
        return body
    except Exception as exc:
        raise CollectorError(f"Agent-Reach bridge doctor failed: {exc}") from exc


def _opencli_doctor() -> dict[str, Any]:
    exe = shutil.which("opencli")
    if not exe:
        return {"installed": False, "connected": False, "status": "NOT_INSTALLED"}
    try:
        returncode, stdout, stderr = _run_cli_text([exe, "doctor"], 30)
        text = (stdout + "\n" + stderr).strip()
        connected = "[OK] Connectivity" in text and "connected" in text.lower()
        extension = "[OK] Extension" in text
        m = re.search(r"opencli\s+v([^\s]+)", text, re.I)
        return {
            "installed": True,
            "connected": connected,
            "extension_connected": extension,
            "status": "CONNECTED" if connected else "NOT_CONNECTED",
            "version": m.group(1) if m else None,
            "raw": text[-4000:],
        }
    except Exception as exc:
        return {"installed": True, "connected": False, "status": "ERROR", "error": str(exc)}


def doctor() -> dict[str, Any]:
    if settings.agent_reach_bridge_url.strip():
        try:
            return _bridge_doctor()
        except Exception as exc:
            return {
                "installed": False,
                "status": "BRIDGE_ERROR",
                "error": str(exc),
                "channels": {},
                "via_bridge": True,
            }

    exe = shutil.which("agent-reach")

    # New flow: OpenCLI provides the real collection backend.
    # Agent-Reach package itself is not required to expose a CLI binary.
    if not exe:
        opencli = _opencli_doctor()

        if opencli.get("installed"):
            connected = bool(opencli.get("connected"))
            channel_status = "ok" if connected else "not_connected"
            return {
                "installed": True,
                "status": "OK" if connected else "NOT_CONNECTED",
                "channels": {
                    "xiaohongshu": {
                        "effective_status": channel_status,
                        "effective_backend": "OpenCLI Browser Bridge",
                        "opencli_connected": connected,
                    },
                    "reddit": {
                        "effective_status": channel_status,
                        "effective_backend": "OpenCLI Browser Bridge",
                        "opencli_connected": connected,
                    },
                    "xianyu": {
                        "effective_status": channel_status,
                        "effective_backend": "OpenCLI Browser Bridge",
                        "opencli_connected": connected,
                    },
                },
                "opencli": opencli,
            }

        return {
            "installed": False,
            "status": "NOT_INSTALLED",
            "channels": {},
        }

    try:
        returncode, stdout, stderr = _run_cli_text([exe, "doctor", "--json"], 60)
        if returncode != 0:
            return {
                "installed": True,
                "status": "ERROR",
                "error": (stderr or stdout).strip(),
                "channels": {},
            }

        body = json.loads(stdout)

        channels = body.get("channels") if isinstance(body, dict) else None
        if not isinstance(channels, dict):
            channels = body if isinstance(body, dict) else {}

        opencli = _opencli_doctor()

        effective = {
            k: dict(v) if isinstance(v, dict) else {"raw": v}
            for k, v in channels.items()
        }

        if opencli.get("connected"):
            for platform in ("xiaohongshu", "reddit", "xianyu"):
                info = dict(effective.get(platform) or {})
                info["effective_status"] = "ok"
                info["effective_backend"] = "OpenCLI Browser Bridge"
                info["opencli_connected"] = True
                effective[platform] = info

        return {
            "installed": True,
            "status": "OK",
            "raw": body,
            "channels": effective,
            "opencli": opencli,
        }

    except Exception as exc:
        return {
            "installed": True,
            "status": "ERROR",
            "error": str(exc),
            "channels": {},
        }

def _parse_output(text: str) -> Any:
    text = text.strip()
    if not text:
        return []
    # JSONL from some CLIs.
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if len(lines) > 1:
        parsed_lines = []
        ok = True
        for line in lines:
            try:
                parsed_lines.append(json.loads(line))
            except Exception:
                ok = False
                break
        if ok and parsed_lines:
            return parsed_lines
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return yaml.safe_load(text)
    except Exception:
        return {"text": text}


def _payload_shape(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return "list[0]"
        first = value[0]
        if isinstance(first, dict):
            return "list[dict] keys=" + ",".join(sorted(str(k) for k in first.keys())[:12])
        return f"list[{type(first).__name__}]"
    if isinstance(value, dict):
        return "dict keys=" + ",".join(sorted(str(k) for k in value.keys())[:16])
    return type(value).__name__


def _is_empty_result_payload(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    if isinstance(value, dict):
        for key in ("items", "results", "posts", "notes", "videos", "feeds", "list"):
            if key in value and isinstance(value.get(key), list):
                return len(value.get(key) or []) == 0
        if "data" in value:
            return _is_empty_result_payload(value.get("data"))
    return False


def _flatten_candidate_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for x in value:
            if isinstance(x, dict):
                nested = _flatten_candidate_items(x)
                out.extend(nested or [x])
        return out
    if isinstance(value, dict):
        # Common normalized envelopes from OpenCLI / public-clis / MCP tools.
        for key in ("items", "results", "posts", "notes", "videos", "feeds", "list"):
            v = value.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        data = value.get("data")
        if isinstance(data, (dict, list)):
            nested = _flatten_candidate_items(data)
            if nested:
                return nested
        content = value.get("content")
        if isinstance(content, list):
            # MCP often wraps JSON text inside content[].text.
            candidates: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                t = part.get("text")
                if isinstance(t, str):
                    nested = _flatten_candidate_items(_parse_output(t))
                    candidates.extend(nested)
            if candidates:
                return candidates
        return [value]
    return []


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _unwrap(raw: dict[str, Any]) -> dict[str, Any]:
    # Preserve original raw payload in evidence; use the most useful nested object for normalization.
    for key in ("noteCard", "note_card", "note", "video", "post", "item"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            merged = dict(raw)
            merged.update(nested)
            return merged
    return raw


def _normalize(platform: str, original: dict[str, Any]) -> CollectedItem | None:
    raw = _unwrap(original)
    title = _first(raw, "title", "name", "subject", "headline", "display_title")
    text = _first(raw, "content", "text", "desc", "description", "summary", "body", "selftext")
    if not title and text:
        title = re.sub(r"\s+", " ", str(text)).strip()[:160]
    if not title:
        return None

    item_id = _first(raw, "id", "item_id", "itemId", "note_id", "noteId", "post_id", "postId", "video_id", "videoId", "aweme_id", "mid", "bvid", "aid")
    url = _first(raw, "url", "link", "web_url", "share_url", "note_url", "permalink", "webpage_url")
    item_id = item_id or _url_item_id(platform, str(url) if url else None)
    if platform == "xiaohongshu" and not url and item_id:
        token = _first(raw, "xsec_token", "xsecToken")
        url = f"https://www.xiaohongshu.com/explore/{item_id}"
        if token:
            url += f"?xsec_token={quote(str(token), safe='')}"
    elif platform == "reddit" and url and str(url).startswith("/"):
        url = "https://www.reddit.com" + str(url)
    elif platform == "reddit" and not url and item_id:
        url = f"https://www.reddit.com/comments/{item_id}/"
    elif platform == "xianyu" and not url and item_id:
        url = f"https://www.goofish.com/item?categoryId=0&id={item_id}"
    elif platform == "bilibili" and not url and item_id:
        sid = str(item_id)
        url = f"https://www.bilibili.com/video/{sid}" if sid.upper().startswith("BV") else f"https://www.bilibili.com/video/av{sid}"
    elif platform == "youtube" and not url and item_id:
        url = f"https://www.youtube.com/watch?v={item_id}"

    if not url and item_id is None:
        return None

    author_raw = _first(raw, "author", "seller", "seller_name", "sellerName", "user", "owner", "nickname", "username", "user_name", "channel", "uploader")
    if isinstance(author_raw, dict):
        author_raw = _first(author_raw, "name", "nickname", "username", "login", "user_name")

    engagement: dict[str, Any] = {}
    aliases = {
        "likes": ("likes", "like_count", "liked_count", "likedCount", "like"),
        "comments": ("comments", "comment_count", "commentCount", "replies", "reply_count"),
        "favorites": ("favorites", "favorite_count", "collect_count", "collected_count", "collectedCount"),
        "shares": ("shares", "share_count", "shareCount"),
        "views": ("views", "view_count", "viewCount", "play", "play_count"),
        "danmaku": ("danmaku", "video_review"),
        "score": ("score", "ups"),
    }
    for normalized_key, keys in aliases.items():
        v = _first(raw, *keys)
        if v is not None:
            engagement[normalized_key] = v

    published = _first(raw, "published_at", "created_at", "createdAt", "created_utc", "publish_time", "pubdate", "time", "date", "timestamp")
    item = CollectedItem(
        platform=platform,
        source_type=f"agent_reach_{platform}",
        source_item_id=str(item_id) if item_id is not None else None,
        source_url=str(url) if url else None,
        title=str(title),
        content=str(text) if text else None,
        author=str(author_raw) if author_raw else None,
        published_at=str(published) if published not in (None, "") else None,
        engagement=engagement,
        raw=original,
        content_kind=str(original.get("_content_kind") or raw.get("content_kind") or "post"),
        parent_source_item_id=str(original.get("_parent_source_item_id")) if original.get("_parent_source_item_id") not in (None, "") else None,
        thread_url=str(original.get("_thread_url")) if original.get("_thread_url") else None,
        published_time_quality=str(original.get("_published_time_quality")) if original.get("_published_time_quality") else None,
    )
    return item


def _channel_info(status: dict[str, Any], platform: str) -> dict[str, Any]:
    channels = status.get("channels") or {}
    if isinstance(channels, dict):
        direct = channels.get(platform)
        if isinstance(direct, dict):
            return direct
        # Some versions use display names / aliases.
        aliases = {
            "xiaohongshu": ("xiaohongshu", "xhs", "小红书"),
            "reddit": ("reddit",),
            "xianyu": ("xianyu", "闲鱼", "goofish"),
            "bilibili": ("bilibili", "bili", "b站"),
            "youtube": ("youtube",),
            "github": ("github",),
        }
        for k, v in channels.items():
            if not isinstance(v, dict):
                continue
            lk = str(k).lower()
            if any(a.lower() in lk for a in aliases.get(platform, (platform,))):
                return v
    return {}


def _run(cmd: list[str], timeout: int = 150) -> Any:
    returncode, stdout, stderr = _run_cli_text(cmd, timeout)
    if returncode != 0:
        raise CollectorError((stderr or stdout or f"command failed: {cmd[0]}").strip())
    return _parse_output(stdout)


def _commands(platform: str, query: str, limit: int, active_backend: str, sort: str | None = None) -> list[list[str]]:
    b = active_backend.lower()
    cmds: list[list[str]] = []
    if platform == "xiaohongshu":
        xhs_sort = str(sort or "comprehensive").strip().lower()
        allowed_sorts = {"comprehensive", "latest", "most-liked", "most-commented", "most-collected"}
        if xhs_sort not in allowed_sorts:
            raise CollectorError(
                "unsupported Xiaohongshu sort: "
                + xhs_sort
                + "; expected one of "
                + ", ".join(sorted(allowed_sorts))
            )
        if "opencli" in b or (not b and shutil.which("opencli")):
            cmd = ["opencli", "xiaohongshu", "search", query, "--limit", str(limit)]
            if xhs_sort != "comprehensive":
                cmd.extend(["--sort", xhs_sort])
            cmd.extend(["-f", "json"])
            cmds.append(cmd)
        if "mcp" in b or "xiaohongshu" in b or (not b and shutil.which("mcporter")):
            call = f'xiaohongshu.search_feeds(keyword: {json.dumps(query, ensure_ascii=False)})'
            cmds.append(["mcporter", "call", call, "--timeout", "120000"])
        if "xhs" in b or (not b and shutil.which("xhs")):
            cmds.append(["xhs", "search", query])
    elif platform == "reddit":
        if "opencli" in b or (not b and shutil.which("opencli")):
            cmds.append(["opencli", "reddit", "search", query, "-f", "yaml"])
        if "rdt" in b or (not b and shutil.which("rdt")):
            cmds.append(["rdt", "search", query, "--limit", str(limit), "--yaml"])
    elif platform == "xianyu":
        if "opencli" in b or (not b and shutil.which("opencli")):
            cmds.append(["opencli", "xianyu", "search", query, "--limit", str(limit), "-f", "json"])
    elif platform == "bilibili":
        if "bili" in b or (not b and shutil.which("bili")):
            cmds.append(["bili", "search", query, "--type", "video", "--max", str(limit), "--yaml"])
        if "opencli" in b or (not b and shutil.which("opencli")):
            # OpenCLI Bilibili search command availability varies. Only attempt if adapter advertises it.
            cmds.append(["opencli", "bilibili", "search", query, "-f", "yaml"])
    elif platform == "youtube":
        if shutil.which("yt-dlp"):
            cmds.append(["yt-dlp", "--dump-json", "--skip-download", f"ytsearch{limit}:{query}"])
    elif platform == "github":
        if shutil.which("gh"):
            cmds.append(["gh", "search", "repos", query, "--limit", str(limit), "--json", "nameWithOwner,url,description,stargazersCount,updatedAt"])
    return [c for c in cmds if shutil.which(c[0])]



def _derived_comment_id(platform: str, parent_id: str | None, author: str | None, text: str) -> str:
    # This is only a deterministic local identity for a real comment when the upstream omits an ID.
    # It never fabricates comment content, author, engagement, or timestamp.
    payload = "\x1f".join([platform, parent_id or "", author or "", re.sub(r"\s+", " ", text).strip()])
    return "comment-hash:" + hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _comment_item(platform: str, parent: CollectedItem, raw: dict[str, Any]) -> CollectedItem | None:
    text = _first(raw, "text", "content", "body", "comment", "desc", "description")
    if text in (None, ""):
        return None
    text = str(text).strip()
    if not text:
        return None
    # OpenCLI may emit placeholders such as "[+46 more replies]". Keep them out at collector level.
    if re.fullmatch(r"\s*\[?\+\d+ more (?:repl(?:y|ies)|top-level comments)\]?\s*", text, flags=re.I):
        return None
    author = _first(raw, "author", "user", "nickname", "username", "user_name")
    if isinstance(author, dict):
        author = _first(author, "name", "nickname", "username", "user_name")
    raw_id = _first(raw, "id", "comment_id", "commentId", "cid", "rpid")
    from ..evidence_identity import account_identity
    account, _ = account_identity(platform, str(author) if author else None, raw)
    sid = str(raw_id) if raw_id not in (None, "") else _derived_comment_id(platform, parent.source_item_id, account, text)
    published = _first(raw, "published_at", "created_at", "createdAt", "created_utc", "time", "timestamp")
    time_quality = "source"
    try:
        if str(published or "").isdigit():
            number = float(published)
            datetime.fromtimestamp(number / 1000 if number > 10_000_000_000 else number, timezone.utc)
        else:
            datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError, OSError):
        time_quality = "inherited_thread"
        published = parent.published_at
    score = _first(raw, "score", "likes", "like_count", "liked_count")
    engagement = {"score": score} if score not in (None, "") else {}
    wrapped = dict(raw)
    wrapped.update({
        "_content_kind": "comment",
        "_parent_source_item_id": parent.source_item_id,
        "_thread_url": parent.source_url,
        "_published_time_quality": time_quality,
    })
    return CollectedItem(
        platform=platform,
        source_type=f"agent_reach_{platform}_comment",
        source_item_id=sid,
        source_url=parent.source_url,
        title=text[:160],
        content=text,
        author=str(author) if author else None,
        published_at=str(published) if published not in (None, "") else None,
        engagement=engagement,
        raw=wrapped,
        content_kind="comment",
        parent_source_item_id=parent.source_item_id,
        thread_url=parent.source_url,
        published_time_quality=time_quality,
    )


def _xianyu_detail_item(parent: CollectedItem) -> CollectedItem:
    if not parent.source_item_id:
        return parent
    try:
        body = _run(
            ["opencli", "xianyu", "item", str(parent.source_item_id), "-f", "json"],
            timeout=max(10, int(settings.xianyu_market_detail_timeout_seconds)),
        )
        raws = _flatten_candidate_items(body)
        detail = raws[0] if raws else (body if isinstance(body, dict) else {})
        if not isinstance(detail, dict) or not detail:
            return parent
        merged = dict(parent.raw or {})
        merged["_xianyu_detail_verified"] = True
        merged["_xianyu_detail"] = detail
        detail_title = _first(detail, "title", "name")
        detail_text = _first(detail, "content", "description", "desc", "text")
        detail_author = _first(detail, "seller", "seller_name", "sellerName", "nick", "nickname")
        if isinstance(detail_author, dict):
            detail_author = _first(detail_author, "name", "nick", "nickname", "user_name")
        return CollectedItem(
            platform=parent.platform,
            source_type=parent.source_type,
            source_item_id=parent.source_item_id,
            source_url=parent.source_url,
            title=str(detail_title or parent.title),
            content=str(detail_text or parent.content or "") or None,
            author=str(detail_author or parent.author) if (detail_author or parent.author) else None,
            published_at=parent.published_at,
            engagement=parent.engagement,
            raw=merged,
            content_kind=parent.content_kind,
            parent_source_item_id=parent.parent_source_item_id,
            thread_url=parent.thread_url,
            published_time_quality=parent.published_time_quality,
        )
    except Exception:
        return parent


def _enrich_xianyu_details(parents: list[CollectedItem], detail_limit: int) -> list[CollectedItem]:
    if not shutil.which("opencli") or detail_limit <= 0:
        return parents

    capped = min(
        len(parents),
        max(0, int(detail_limit)),
        max(1, int(settings.xianyu_market_detail_limit)),
    )
    indexes = [index for index, parent in enumerate(parents[:capped]) if parent.source_item_id]
    if not indexes:
        return parents

    out = list(parents)
    workers = min(max(1, int(settings.xianyu_market_detail_workers)), len(indexes))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="xianyu-detail") as pool:
        future_map = {pool.submit(_xianyu_detail_item, parents[index]): index for index in indexes}
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                out[index] = future.result()
            except Exception:
                out[index] = parents[index]
    return out


def _enrich_opencli(platform: str, parents: list[CollectedItem], detail_limit: int, comments_limit: int) -> list[CollectedItem]:
    """Read real thread/comment data through the connected OpenCLI Browser Bridge.

    This does not read cookies itself. It only invokes OpenCLI after the user's browser bridge/login
    is already working. Xiaohongshu comments require the full signed search URL; Reddit read uses
    the real post id. Failures on one thread do not fabricate or replace data.
    """
    if not shutil.which("opencli") or detail_limit <= 0:
        return []
    from ..services.intent import classify_intent
    out: list[CollectedItem] = []
    selected = sorted(parents, key=lambda item: -classify_intent(item.title, item.content).demand_weight)[:detail_limit]
    for parent in selected:
        parent.source_item_id = parent.source_item_id or _url_item_id(platform, parent.source_url)
        metadata: dict[str, Any] = {"observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        parent.raw = {**parent.raw, "_enrichment": metadata}
        timeout = max(10, int(settings.demand_detail_timeout_seconds))
        raw_comments: list[dict[str, Any]] = []
        try:
            if platform == "xiaohongshu":
                if not parent.source_url or "xsec_token=" not in parent.source_url:
                    metadata.update(detail_status="NOT_AVAILABLE", comments_status="NOT_AVAILABLE", error="signed source URL is unavailable")
                    continue
                body = _run(["opencli", "xiaohongshu", "note", parent.source_url, "-f", "json"], timeout=timeout)
                rows = _flatten_candidate_items(body)
                detail = {str(row["field"]): row.get("value") for row in rows if "field" in row}
                if "content" not in detail and "desc" not in detail and "description" not in detail:
                    raise CollectorError("Xiaohongshu note returned no body field")
                content = _first(detail, "content", "desc", "description")
                parent.raw["_detail"] = detail
                if content:
                    parent.content = str(content)
                    parent.title = str(detail.get("title") or parent.title)
                    parent.author = str(detail.get("author") or parent.author or "") or None
                metadata["detail_status"] = "SUCCESS" if content else "EMPTY"
            elif platform == "reddit":
                if not parent.source_item_id:
                    metadata.update(detail_status="NOT_AVAILABLE", comments_status="NOT_AVAILABLE", error="source post ID is unavailable")
                    continue
                body = _run(["opencli", "reddit", "read", parent.source_item_id, "--limit", str(max(1, comments_limit)), "--depth", "2", "--max-length", "10000", "-f", "json"], timeout=timeout)
                rows = _flatten_candidate_items(body)
                roots = [row for row in rows if str(row.get("type") or "").upper() == "POST"]
                if len(roots) != 1:
                    raise CollectorError("Reddit read did not return exactly one POST root")
                root = roots[0]
                if parent.author and root.get("author") and parent.author.strip().lower() != str(root["author"]).strip().lower():
                    raise CollectorError("Reddit read author does not match the source post")
                parent.raw["_detail"] = root
                content = _first(root, "selftext", "body")
                if content is None:
                    # OpenCLI's POST text is title + blank line + selftext.
                    # A root containing only a title cannot supply missing body text.
                    _, separator, body_text = str(root.get("text") or "").partition("\n\n")
                    content = body_text if separator else None
                truncated = "[truncated]" in str(content or "")
                metadata["body_truncated"] = truncated
                if content and (not truncated or not parent.content or len(str(content)) > len(parent.content)):
                    parent.content = str(content)
                metadata["detail_status"] = "SUCCESS" if content else "EMPTY"
                raw_comments = [row for row in rows if str(row.get("type") or "").upper().startswith("L")]
            else:
                continue
        except Exception as exc:
            metadata.update(detail_status="FAILED", detail_error=redact_error(exc))
        if comments_limit <= 0:
            metadata["comments_status"] = "NOT_REQUESTED"
            continue
        try:
            if platform == "xiaohongshu":
                body = _run(["opencli", "xiaohongshu", "comments", str(parent.source_url), "--limit", str(min(50, comments_limit)), "--with-replies", "true", "-f", "json"], timeout=timeout)
                raw_comments = _flatten_candidate_items(body)
            elif metadata.get("detail_status") == "FAILED":
                raise CollectorError("comments unavailable because Reddit read failed")
            taken = 0
            for raw in raw_comments:
                ci = _comment_item(platform, parent, raw)
                if ci is None:
                    continue
                ci.validate()
                out.append(ci)
                taken += 1
                if taken >= comments_limit:
                    break
            metadata.update(comments_status="SUCCESS" if taken else "EMPTY", comments_retained=taken)
        except Exception as exc:
            metadata.update(comments_status="FAILED", comments_error=redact_error(exc))
    return out


def collect(
    platform: str,
    query: str,
    limit: int = 20,
    *,
    enrich: bool = False,
    detail_limit: int = 8,
    comments_limit: int = 15,
    sort: str | None = None,
) -> list[CollectedItem]:
    """Collect real platform results through Agent-Reach-selected upstream tools.

    Agent-Reach is a router/installer rather than a data proxy. We run its doctor first,
    then invoke only documented read/search commands. No login is automated and no
    browser cookies are read by this adapter itself.
    """
    if settings.agent_reach_bridge_url.strip():
        base = settings.agent_reach_bridge_url.strip().rstrip("/")
        try:
            with httpx.Client(timeout=190, headers=_bridge_headers()) as client:
                payload = {"platform": platform, "query": query, "limit": limit, "enrich": enrich, "detail_limit": detail_limit, "comments_limit": comments_limit}
                if sort:
                    payload["sort"] = sort
                resp = client.post(base + "/collect", json=payload)
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:
            raise CollectorError(f"Agent-Reach bridge collection failed: {exc}") from exc
        out: list[CollectedItem] = []
        for raw in body.get("items", []):
            if not isinstance(raw, dict):
                continue
            item = CollectedItem(**raw)
            item.validate()
            out.append(item)
        if not out:
            raise CollectorError("Agent-Reach bridge returned no real items")
        return out

    status = doctor()
    if not status.get("installed"):
        raise CollectorError("Agent-Reach is not installed")
    info = _channel_info(status, platform)
    active_backend = str(info.get("active_backend") or info.get("effective_backend") or info.get("backend") or "")
    commands = _commands(platform, query, limit, active_backend, sort=sort)
    if not commands:
        detail = info.get("message") or info.get("status") or "no documented backend command is available"
        raise CollectorError(f"Agent-Reach {platform} is not ready: {detail}")

    errors: list[str] = []
    for cmd in commands:
        try:
            body = _run(cmd)
            raw_items = _flatten_candidate_items(body)
            if not raw_items and _is_empty_result_payload(body):
                # A successful OpenCLI command returning [] (or an empty normalized
                # envelope) is a legitimate empty search result, not a parser failure.
                return []
            out: list[CollectedItem] = []
            for raw in raw_items:
                item = _normalize(platform, raw)
                if item:
                    item.validate()
                    out.append(item)
            if out:
                search_items = out[:limit]
                if enrich and cmd[0] == "opencli":
                    if platform in {"xiaohongshu", "reddit"}:
                        search_items.extend(_enrich_opencli(platform, search_items, max(0, int(detail_limit)), max(0, int(comments_limit))))
                    elif platform == "xianyu":
                        search_items = _enrich_xianyu_details(search_items, max(0, int(detail_limit)))
                return search_items
            errors.append(
                f"{cmd[0]} returned no parseable real items "
                f"(raw_items={len(raw_items)}, payload={_payload_shape(body)})"
            )
        except Exception as exc:
            errors.append(f"{cmd[0]}: {exc}")
    raise CollectorError("Agent-Reach real collection failed: " + " | ".join(errors))



def publish_xianyu_listing(
    title: str,
    description: str,
    price: float,
    condition: str,
    category: str,
    image_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Publish a real Xianyu listing through the authenticated OpenCLI browser adapter."""
    if not shutil.which("opencli"):
        raise CollectorError("OpenCLI is not installed")
    if not title.strip() or not description.strip() or float(price) <= 0 or not category.strip():
        raise CollectorError("Xianyu publish fields are incomplete")
    if condition not in {"全新", "几乎全新", "轻微使用", "明显使用", "老旧"}:
        raise CollectorError("unsupported Xianyu condition")
    cmd = [
        "opencli", "xianyu", "publish",
        title.strip(), description.strip(), f"{float(price):.2f}", condition, category.strip(),
    ]
    paths = [str(x).strip() for x in (image_paths or []) if str(x).strip()]
    if paths:
        cmd.extend(["--images", ",".join(paths)])
    cmd.extend(["-f", "json"])
    body = _run(cmd, timeout=180)
    if isinstance(body, dict):
        return body
    return {"data": body}
