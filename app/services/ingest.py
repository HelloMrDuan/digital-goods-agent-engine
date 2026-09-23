from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..collectors.base import CollectedItem
from ..db import conn, dumps, now_iso, stable_hash
from .intent import classify_intent

_URL_RE = re.compile(r"https?://\S+", re.I)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)

# Deterministic signals only. They are not treated as market facts; they help expose how
# direct a post's wording is about a need/problem instead of a launch/showcase.
_POSITIVE = {
    "求": 12, "求助": 18, "请问": 8, "有没有": 16, "有无": 14, "哪里有": 16,
    "想找": 18, "需要": 12, "需求": 10, "推荐": 10, "怎么": 8, "如何": 8,
    "能不能": 10, "可以吗": 8, "有没有办法": 18, "解决": 6, "替代": 8,
    "looking for": 20, "need a": 16, "need an": 16, "anyone know": 14,
    "recommend": 10, "how do i": 12, "how can i": 12, "is there": 10,
}

_TRACKING_KEYS = {"spm", "from", "from_source", "share_source", "share_medium", "source", "ref", "ref_src", "feature"}

def canonicalize_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
        query = []
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            lk = k.lower()
            if lk.startswith("utm_") or lk in _TRACKING_KEYS:
                continue
            query.append((k, v))
        # stable ordering prevents query-order duplicates; fragment is never identity.
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))
    except Exception:
        return str(url).strip()

def canonical_identity(item: CollectedItem) -> str:
    # Platform item IDs are the strongest identity and must not be split by changing share/tracking URLs.
    if item.source_item_id not in (None, ""):
        return stable_hash("item-v2", item.platform, str(item.source_item_id).strip())
    return stable_hash("url-v2", item.platform, canonicalize_url(item.source_url))

_NEGATIVE = {
    "发布": 10, "上线": 8, "开源项目": 14, "项目发布": 18, "新品": 8, "教程": 5,
    "分享一下": 8, "我做了": 10, "正式推出": 14, "released": 12, "launch": 12,
    "introducing": 14, "showcase": 12, "announcement": 12,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.isdigit():
            n = int(s)
            if n > 10_000_000_000:
                n /= 1000
            return datetime.fromtimestamp(float(n))
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def normalize_content(title: str | None, content: str | None) -> str:
    text = f"{title or ''} {content or ''}".lower()
    text = _URL_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:6000]


def content_fingerprint(item: CollectedItem) -> str:
    normalized = normalize_content(item.title, item.content)
    if item.content_kind == "comment":
        # Identical request wording from different real users is multiple demand signals, not a mirror copy.
        return stable_hash("comment-content-v1", item.platform, item.parent_source_item_id, item.author, normalized)
    return stable_hash("content-v1", normalized)


def demand_signal_score(item: CollectedItem) -> float:
    text = normalize_content(item.title, item.content)
    score = 0.0
    for phrase, weight in _POSITIVE.items():
        if phrase in text:
            score += weight
    for phrase, weight in _NEGATIVE.items():
        if phrase in text:
            score -= weight
    # A question mark is weak evidence of an explicit question, never enough on its own.
    raw_text = f"{item.title or ''} {item.content or ''}"
    if "?" in raw_text or "？" in raw_text:
        score += 6
    return round(max(0.0, min(100.0, score)), 2)


def evidence_quality_score(item: CollectedItem) -> tuple[float, str]:
    score = 0.0
    if item.source_url:
        score += 15
    if item.source_item_id:
        score += 10
    title_len = len((item.title or "").strip())
    if title_len >= 8:
        score += 10
    elif title_len >= 3:
        score += 5
    if item.content and len(item.content.strip()) >= 30:
        score += 15
    elif item.content and len(item.content.strip()) >= 8:
        score += 8
    if item.author:
        score += 10
    published_ok = _parse_dt(item.published_at) is not None
    time_quality = item.published_time_quality or ("source" if published_ok else "fallback")
    if published_ok and time_quality == "source":
        score += 25
    elif published_ok and time_quality == "inherited_thread":
        score += 10
    if item.engagement:
        score += 5
    if isinstance(item.raw, dict) and item.raw:
        score += 10
    return round(min(100.0, score), 2), time_quality


def _record_observation(c, evidence_id: int, source_id: int, observed_at: str, item: CollectedItem, raw_hash: str) -> None:
    c.execute(
        "INSERT INTO evidence_observations(evidence_id,source_id,observed_at,engagement_json,raw_hash) VALUES(?,?,?,?,?)",
        (evidence_id, source_id, observed_at, dumps(item.engagement), raw_hash),
    )
    c.execute(
        """INSERT INTO evidence_sources(evidence_id,source_id,first_seen_at,last_seen_at,observation_count)
           VALUES(?,?,?,?,1)
           ON CONFLICT(evidence_id,source_id) DO UPDATE SET
             last_seen_at=excluded.last_seen_at,
             observation_count=evidence_sources.observation_count+1""",
        (evidence_id, source_id, observed_at, observed_at),
    )


def insert_evidence(source: dict[str, Any], item: CollectedItem) -> tuple[int, bool]:
    """Insert or observe one real item.

    Exact source identity controls row uniqueness. Content fingerprint is stored separately so
    copied/mirrored posts can be de-inflated in trend calculations without destroying provenance.
    """
    item.validate()
    observed_at = now_iso()
    canonical_key = canonical_identity(item)
    raw_hash = stable_hash(dumps(item.raw))
    fingerprint = content_fingerprint(item)
    quality, time_quality = evidence_quality_score(item)
    demand_signal = demand_signal_score(item)
    intent = classify_intent(item.title, item.content, item.content_kind)
    with conn() as c:
        existing = c.execute("SELECT id FROM evidence WHERE canonical_key=?", (canonical_key,)).fetchone()
        if not existing and item.source_item_id not in (None, ""):
            existing = c.execute("SELECT id FROM evidence WHERE platform=? AND source_item_id=? ORDER BY id LIMIT 1", (item.platform, str(item.source_item_id))).fetchone()
        if not existing and item.source_url:
            existing = c.execute("SELECT id FROM evidence WHERE platform=? AND source_url=? ORDER BY id LIMIT 1", (item.platform, item.source_url)).fetchone()
        if existing:
            eid = int(existing["id"])
            _record_observation(c, eid, int(source["id"]), observed_at, item, raw_hash)
            owner = c.execute("SELECT id FROM evidence WHERE canonical_key=?", (canonical_key,)).fetchone()
            if not owner or int(owner["id"]) == eid:
                c.execute("UPDATE evidence SET canonical_key=? WHERE id=?", (canonical_key, eid))
            c.execute(
                """UPDATE evidence SET last_seen_at=?, observation_count=coalesce(observation_count,1)+1,
                   engagement_json=?, raw_json=?, raw_hash=?, content_fingerprint=?, evidence_quality_score=?,
                   published_time_quality=?, demand_signal_score=?,content_kind=?,parent_source_item_id=?,thread_url=?,
                   intent_class=?,intent_confidence=?,intent_weight=?,intent_reason_json=? WHERE id=?""",
                (observed_at, dumps(item.engagement), dumps(item.raw), raw_hash, fingerprint, quality, time_quality, demand_signal,
                 item.content_kind, item.parent_source_item_id, item.thread_url, intent.label, intent.confidence, intent.demand_weight, dumps(intent.reason_codes), eid),
            )
            c.execute(
                """UPDATE opportunities SET validation_status='STALE'
                   WHERE validation_status='PASSED' AND id IN (
                       SELECT opportunity_id FROM opportunity_evidence
                       WHERE evidence_id=? AND relation_type='demand'
                   )""",
                (eid,),
            )
            return eid, False

        cur = c.execute(
            """
            INSERT INTO evidence(
                source_id, platform, source_type, source_item_id, source_url, title, content,
                author, published_at, collected_at, engagement_json, raw_json, raw_hash, canonical_key,
                content_fingerprint,evidence_quality_score,published_time_quality,demand_signal_score,
                content_kind,parent_source_item_id,thread_url,intent_class,intent_confidence,intent_weight,intent_reason_json,
                last_seen_at, observation_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            (
                source["id"], item.platform, item.source_type, item.source_item_id, item.source_url,
                item.title.strip(), item.content, item.author, item.published_at, observed_at,
                dumps(item.engagement), dumps(item.raw), raw_hash, canonical_key,
                fingerprint, quality, time_quality, demand_signal, item.content_kind, item.parent_source_item_id, item.thread_url,
                intent.label, intent.confidence, intent.demand_weight, dumps(intent.reason_codes), observed_at,
            ),
        )
        eid = int(cur.lastrowid)
        _record_observation(c, eid, int(source["id"]), observed_at, item, raw_hash)
        return eid, True


def ingest_items(source: dict[str, Any], items: list[CollectedItem]) -> dict[str, Any]:
    inserted_ids: list[int] = []
    observed_ids: list[int] = []
    duplicates = 0
    rejected = 0
    for item in items:
        try:
            eid, inserted = insert_evidence(source, item)
        except ValueError:
            rejected += 1
            continue
        observed_ids.append(eid)
        if inserted:
            inserted_ids.append(eid)
        else:
            duplicates += 1
    return {
        "fetched": len(items),
        "inserted": len(inserted_ids),
        "duplicates": duplicates,
        "observations": len(observed_ids),
        "rejected_invalid": rejected,
        "evidence_ids": inserted_ids,
        "observed_evidence_ids": observed_ids,
    }


def backfill_intents() -> dict[str, int]:
    """Classify evidence created by older REAL_DATA_ONLY releases.

    This is a deterministic migration only; it does not call any model and does not create evidence.
    """
    with conn() as c:
        rows = c.execute(
            """SELECT id,title,content,coalesce(content_kind,'post') content_kind
               FROM evidence WHERE intent_class IS NULL OR intent_class='' OR intent_class='unclassified'"""
        ).fetchall()
        updated = 0
        for row in rows:
            decision = classify_intent(row["title"], row["content"], row["content_kind"] or "post")
            c.execute(
                """UPDATE evidence SET intent_class=?,intent_confidence=?,intent_weight=?,intent_reason_json=? WHERE id=?""",
                (decision.label, decision.confidence, decision.demand_weight, dumps(decision.reason_codes), int(row["id"])),
            )
            updated += 1
    return {"updated": updated}
