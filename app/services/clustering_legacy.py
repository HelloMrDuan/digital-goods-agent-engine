from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import BASE_DIR, settings
from ..db import conn, dumps, loads, now_iso, row_to_dict, stable_hash

_METHOD = "real_evidence_tfidf_v5_content_intent_gate"
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_+.-]{1,}", re.I)
_HTML_RE = re.compile(r"<[^>]+>")

@lru_cache(maxsize=1)
def _semantic_aliases() -> dict[str, list[str]]:
    path = BASE_DIR / "config" / "semantic_aliases.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): [str(x).lower() for x in v if str(x).strip()] for k, v in raw.items() if isinstance(v, list)}
    except Exception:
        return {}

def _concept_tokens(cleaned_text: str) -> set[str]:
    out: set[str] = set()
    for concept, aliases in _semantic_aliases().items():
        for alias in aliases:
            if not alias:
                continue
            if alias.isascii() and len(alias) <= 3:
                if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", cleaned_text, re.I):
                    out.add("@" + concept); break
            elif alias in cleaned_text:
                out.add("@" + concept); break
    return out

_STOP = {
    "一个", "这个", "那个", "什么", "怎么", "如何", "有没有", "没有", "可以", "能不能", "需要", "求助", "请问", "推荐",
    "大家", "有人", "哪个", "哪些", "一下", "现在", "目前", "有没有人", "工具", "模板", "分享", "求", "问题", "使用",
    "the", "and", "for", "with", "this", "that", "what", "how", "any", "need", "looking", "look", "best", "good", "help",
    "template", "templates", "tool", "tools", "recommend", "recommendation", "recommendations",
}


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        try:
            n = int(s)
            if len(s) == 8 and 19000101 <= n <= 29991231:
                return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
            if n > 10_000_000_000:
                n = n / 1000
            return datetime.fromtimestamp(float(n), tz=timezone.utc)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _event_dt(row: dict[str, Any]) -> datetime:
    return _parse_dt(row.get("published_at")) or _parse_dt(row.get("collected_at")) or datetime.now(timezone.utc)


def _clean_text(value: Any) -> str:
    text = _HTML_RE.sub(" ", str(value or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    text = _clean_text(text)
    out: set[str] = set(_concept_tokens(text))
    for w in _LATIN_RE.findall(text):
        w = w.lower().strip("._-+")
        if len(w) >= 2 and w not in _STOP:
            out.add(w)
    for seq in _CJK_RE.findall(text):
        if len(seq) <= 8 and seq not in _STOP:
            out.add(seq)
        for n in (2, 3):
            if len(seq) < n:
                continue
            for i in range(len(seq) - n + 1):
                g = seq[i:i+n]
                if g not in _STOP:
                    out.add(g)
    return out


def _vector(title: str, content: str, idf: dict[str, float]) -> dict[str, float]:
    title_t = _tokens(title)
    body_t = _tokens((content or "")[:1400])
    v: dict[str, float] = {}
    for t in body_t:
        weight = 1.25 if t.startswith("@") else 0.45
        v[t] = idf.get(t, 1.0) * weight
    for t in title_t:
        weight = 3.2 if t.startswith("@") else 1.8
        v[t] = max(v.get(t, 0.0), idf.get(t, 1.0) * weight)
    return v


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    if dot <= 0:
        return 0.0
    na = math.sqrt(sum(v*v for v in a.values()))
    nb = math.sqrt(sum(v*v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


_GENERIC_CONCEPTS = {"@template", "@spreadsheet", "@automation", "@document"}
_BROAD_CONTEXT_CONCEPTS = {"@small_business", "@content_creator", "@social_media"}
_STRONG_TOPIC_CONCEPTS = {
    "@payroll", "@attendance", "@scheduling", "@inventory", "@expense", "@resume",
    "@job_search", "@short_video", "@copywriting", "@ai_agent", "@prompt", "@notion",
    "@presentation", "@study_plan", "@flashcard", "@finance", "@crm", "@quotation",
    "@contract", "@calendar",
}


def _concept_keys(vector: dict[str, float]) -> set[str]:
    return {key for key in vector if key.startswith("@")}


def _shared_business_topic(a: dict[str, float], b: dict[str, float]) -> bool:
    shared = _concept_keys(a) & _concept_keys(b)
    if shared & _STRONG_TOPIC_CONCEPTS:
        return True
    # Generic concepts can corroborate each other only when there is more than one
    # shared product concept, e.g. spreadsheet+template. A lone "automation" never merges.
    return len(shared & _GENERIC_CONCEPTS) >= 2


def _topic_compatible(a: dict[str, float], b: dict[str, float], cosine: float) -> bool:
    """Hard semantic guard applied before any cluster merge.

    V7 only consulted semantic topics when cosine fell below the normal threshold.
    That allowed unrelated workflow requests to merge whenever generic wording pushed
    cosine above the threshold. V8 requires a compatible business topic regardless of
    cosine, except for near-duplicate text with very high lexical similarity.
    """
    ca = _concept_keys(a)
    cb = _concept_keys(b)
    if not ca or not cb:
        return cosine >= 0.72

    specific_a = ca - _GENERIC_CONCEPTS - _BROAD_CONTEXT_CONCEPTS
    specific_b = cb - _GENERIC_CONCEPTS - _BROAD_CONTEXT_CONCEPTS

    if specific_a and specific_b:
        return bool(specific_a & specific_b)

    if _shared_business_topic(a, b):
        return True

    # Broad audience labels such as small_business are never sufficient on their own.
    # Generic automation/template overlap is also insufficient unless there are at
    # least two shared generic product concepts.
    return cosine >= 0.72


def _centroid(vecs: list[dict[str, float]]) -> dict[str, float]:
    if not vecs:
        return {}
    acc: dict[str, float] = defaultdict(float)
    for v in vecs:
        for k, value in v.items():
            acc[k] += value
    n = float(len(vecs))
    return {k: v / n for k, v in acc.items()}


def _load_evidence() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(settings.cluster_window_days, settings.trend_window_days + 14))
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM evidence ORDER BY id DESC LIMIT ?",
            (max(50, min(settings.cluster_max_evidence, 5000)),),
        ).fetchall()
        source_rows = c.execute("SELECT id,name,platform,query,config_json FROM sources").fetchall()
        link_rows = c.execute("SELECT evidence_id,source_id FROM evidence_sources").fetchall()
    sources = {int(r["id"]): {**dict(r), "config": loads(r["config_json"], {}) or {}} for r in source_rows}
    links: dict[int, list[int]] = defaultdict(list)
    for r in link_rows:
        links[int(r["evidence_id"])].append(int(r["source_id"]))
    demand: list[dict[str, Any]] = []
    support: list[dict[str, Any]] = []
    for r in rows:
        d = row_to_dict(r) or {}
        dt = _event_dt(d)
        if dt < cutoff:
            continue
        linked = [sources[sid] for sid in links.get(int(d["id"]), []) if sid in sources]
        if not linked:
            s = sources.get(int(d["source_id"]))
            linked = [s] if s else []
        roles = {str((x.get("config") or {}).get("role") or "demand").lower() for x in linked}
        groups = set()
        priorities = set()
        for x in linked:
            cfg = x.get("config") or {}
            role = str(cfg.get("role") or "demand").lower()
            platform = str(x.get("platform") or "").lower()
            group = cfg.get("business_group")
            priority = cfg.get("priority")
            if not group:
                group = "auxiliary" if platform == "v2ex" else {"demand":"core_demand","trend":"trend_support","supply":"supply_intel"}.get(role,"auxiliary")
            if not priority:
                priority = "P3" if platform == "v2ex" else {"demand":"P0","trend":"P1","supply":"P2"}.get(role,"P3")
            groups.add(str(group))
            priorities.add(str(priority).upper())
        d["_event_dt"] = dt
        d["_sources"] = linked
        d["_roles"] = roles or {"demand"}
        d["_business_groups"] = groups or {"auxiliary"}
        d["_priorities"] = priorities or {"P3"}
        # Business-priority gate:
        # - only core_demand + demand evidence can create demand clusters;
        # - only trend_support + trend evidence can support a cluster;
        # - auxiliary (e.g. V2EX) and supply never inflate business-demand rankings.
        if "demand" in d["_roles"] and "core_demand" in d["_business_groups"]:
            if str(d.get("intent_class") or "unclassified") in {"explicit_demand", "probable_demand"}:
                demand.append(d)
        elif "trend" in d["_roles"] and "trend_support" in d["_business_groups"]:
            support.append(d)
    return demand, support


def _build_vectors(rows: list[dict[str, Any]]) -> tuple[list[dict[str, float]], dict[str, float], Counter[str]]:
    doc_sets = [_tokens((r.get("title") or "") + " " + (r.get("content") or "")[:1000]) for r in rows]
    df = Counter(t for toks in doc_sets for t in toks)
    n = max(1, len(rows))
    idf = {t: math.log((n + 1) / (cnt + 1)) + 1.0 for t, cnt in df.items()}
    vecs = [_vector(r.get("title") or "", r.get("content") or "", idf) for r in rows]
    return vecs, idf, df


def _cluster_rows(rows: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], dict[int, dict[str, float]], dict[str, float]]:
    """Deterministic compact clustering.

    V2 used connected components, which can chain A~B and B~C even when A and C are unrelated.
    V3 assigns each item to the best existing centroid and enforces a compactness floor against
    current members. This is slower but materially safer for business-demand clusters.
    """
    if not rows:
        return [], {}, {}
    vecs, idf, _ = _build_vectors(rows)
    by_id = {int(r["id"]): vecs[i] for i, r in enumerate(rows)}
    threshold = float(settings.cluster_similarity_threshold)
    compact_floor = max(0.16, threshold * 0.68)
    semantic_floor = max(0.14, threshold * 0.42)
    clusters: list[list[dict[str, Any]]] = []
    cluster_vecs: list[list[dict[str, float]]] = []

    # High-quality, explicit-demand evidence seeds clusters first; this reduces order sensitivity.
    ordered = sorted(
        rows,
        key=lambda r: (
            -float(r.get("demand_signal_score") or 0),
            -float(r.get("evidence_quality_score") or 0),
            -r["_event_dt"].timestamp(),
            int(r["id"]),
        ),
    )
    for row in ordered:
        v = by_id[int(row["id"])]
        best_idx = None
        best_score = 0.0
        for idx, members in enumerate(clusters):
            cent = _centroid(cluster_vecs[idx])
            sim = _cosine(v, cent)
            sample_vecs = cluster_vecs[idx][:12]
            if not sample_vecs:
                continue

            member_sims = [_cosine(v, member_vector) for member_vector in sample_vecs]
            if not all(
                _topic_compatible(v, member_vector, member_sim)
                for member_vector, member_sim in zip(sample_vecs, member_sims)
            ):
                continue

            semantic_topic_match = all(
                _shared_business_topic(v, member_vector) for member_vector in sample_vecs
            )
            if sim < threshold and not (semantic_topic_match and sim >= semantic_floor):
                continue

            # Avoid transitive chain pollution. Semantic lowering is allowed only when
            # every sampled member shares the same specific business concept.
            min_member_sim = min(member_sims, default=sim)
            required_floor = semantic_floor if semantic_topic_match else compact_floor
            if min_member_sim < required_floor:
                continue
            if sim > best_score:
                best_idx, best_score = idx, sim
        if best_idx is None:
            clusters.append([row])
            cluster_vecs.append([v])
        else:
            clusters[best_idx].append(row)
            cluster_vecs[best_idx].append(v)

    clusters.sort(key=lambda g: (-len(g), min(int(x["id"]) for x in g)))
    return clusters, by_id, idf


def _representative(cluster: list[dict[str, Any]], vectors: dict[int, dict[str, float]]) -> dict[str, Any]:
    if len(cluster) == 1:
        return cluster[0]
    cent = _centroid([vectors[int(r["id"])] for r in cluster])
    ranked = sorted(
        cluster,
        key=lambda r: (
            _cosine(vectors[int(r["id"])], cent),
            float(r.get("intent_weight") or 0),
            float(r.get("demand_signal_score") or 0) / 100.0,
            float(r.get("evidence_quality_score") or 0) / 100.0,
            r["_event_dt"].timestamp(),
        ),
        reverse=True,
    )
    return ranked[0]


def _signature(cluster: list[dict[str, Any]]) -> str:
    cnt = Counter()
    for r in cluster:
        signal_weight = (1.0 + min(1.0, float(r.get("demand_signal_score") or 0) / 40.0)) * max(0.55, float(r.get("intent_weight") or 0.55))
        for t in _tokens(r.get("title") or ""):
            cnt[t] += signal_weight
    top = [t for t, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[:12]]
    return "|".join(top) or f"evidence-{min(int(r['id']) for r in cluster)}"


def _existing_for_cluster(c, evidence_ids: list[int], candidate_key: str) -> int | None:
    if evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = c.execute(
            f"""SELECT oe.opportunity_id, COUNT(*) overlap_count,
                       (SELECT COUNT(*) FROM opportunity_evidence x WHERE x.opportunity_id=oe.opportunity_id AND x.relation_type='demand') old_count
                FROM opportunity_evidence oe
                JOIN opportunities o ON o.id=oe.opportunity_id
                WHERE oe.evidence_id IN ({placeholders}) AND oe.relation_type='demand'
                  AND o.cluster_method IN (?,?,?,?,?)
                GROUP BY oe.opportunity_id ORDER BY overlap_count DESC""",
            tuple(evidence_ids) + (_METHOD, "real_evidence_tfidf_v4_business_priority", "real_evidence_tfidf_v3", "real_evidence_tfidf_v2", "real_evidence_tfidf_v1"),
        ).fetchall()
        best: tuple[float, int] | None = None
        new_set_size = len(evidence_ids)
        for row in rows:
            overlap = int(row["overlap_count"])
            old_count = max(1, int(row["old_count"] or 0))
            union = new_set_size + old_count - overlap
            jaccard = overlap / max(1, union)
            containment = overlap / max(1, min(new_set_size, old_count))
            score = max(jaccard, containment * 0.82)
            if overlap >= 1 and (best is None or score > best[0]):
                best = (score, int(row["opportunity_id"]))
        if best and best[0] >= 0.32:
            return best[1]
    row = c.execute("SELECT id FROM opportunities WHERE candidate_key=?", (candidate_key,)).fetchone()
    return int(row["id"]) if row else None


def _numeric_total(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        s = value.strip().lower().replace(",", "")
        mult = 1.0
        if s.endswith("k"):
            mult, s = 1000.0, s[:-1]
        elif s.endswith("m"):
            mult, s = 1_000_000.0, s[:-1]
        elif s.endswith("万"):
            mult, s = 10_000.0, s[:-1]
        try:
            return max(0.0, float(s) * mult)
        except Exception:
            return 0.0
    return 0.0


def _effective_signal_dates(evidences: list[dict[str, Any]]) -> list[tuple[str, datetime]]:
    """Collapse exact copied/mirrored content for counting, keeping the earliest observed event."""
    by_fp: dict[str, datetime] = {}
    for e in evidences:
        fp = str(e.get("content_fingerprint") or e.get("canonical_key") or e.get("id"))
        dt = _event_dt(e)
        old = by_fp.get(fp)
        if old is None or dt < old:
            by_fp[fp] = dt
    return sorted(by_fp.items(), key=lambda x: x[1])


def calculate_trend(opportunity_id: int, calculated_at: datetime | None = None) -> dict[str, Any]:
    now = calculated_at or datetime.now(timezone.utc)
    with conn() as c:
        demand_rows = c.execute(
            """SELECT e.* FROM opportunity_evidence oe JOIN evidence e ON e.id=oe.evidence_id
               WHERE oe.opportunity_id=? AND oe.relation_type='demand'""",
            (opportunity_id,),
        ).fetchall()
        support_rows = c.execute(
            """SELECT e.* FROM opportunity_evidence oe JOIN evidence e ON e.id=oe.evidence_id
               WHERE oe.opportunity_id=? AND oe.relation_type='support'""",
            (opportunity_id,),
        ).fetchall()
        source_rows = c.execute(
            """SELECT es.evidence_id,s.id,s.name,s.platform FROM evidence_sources es
               JOIN sources s ON s.id=es.source_id
               JOIN opportunity_evidence oe ON oe.evidence_id=es.evidence_id
               WHERE oe.opportunity_id=? AND oe.relation_type='demand'""",
            (opportunity_id,),
        ).fetchall()
    evidences = [row_to_dict(r) or {} for r in demand_rows]
    supports = [row_to_dict(r) or {} for r in support_rows]
    sources_by_e: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in source_rows:
        sources_by_e[int(r["evidence_id"])].append(dict(r))

    cur7_start = now - timedelta(days=7)
    prev7_start = now - timedelta(days=14)
    cur30_start = now - timedelta(days=30)
    cur3_start = now - timedelta(days=3)
    prior4_start = now - timedelta(days=7)
    effective = _effective_signal_dates(evidences)
    dates = [dt for _, dt in effective]
    current7 = sum(1 for dt in dates if cur7_start <= dt <= now)
    previous7 = sum(1 for dt in dates if prev7_start <= dt < cur7_start)
    current30 = sum(1 for dt in dates if cur30_start <= dt <= now)
    current3 = sum(1 for dt in dates if cur3_start <= dt <= now)
    prior4 = sum(1 for dt in dates if prior4_start <= dt < cur3_start)
    growth = ((current7 - previous7) / previous7 * 100.0) if previous7 > 0 else None
    is_new = 1 if previous7 == 0 and current7 > 0 else 0
    prior4_rate = prior4 / 4.0
    current3_rate = current3 / 3.0
    acceleration = ((current3_rate - prior4_rate) / prior4_rate * 100.0) if prior4_rate > 0 else None

    platforms = Counter(e.get("platform") or "unknown" for e in evidences)
    support_platforms = {str(e.get("platform") or "unknown") for e in supports}
    source_counts: Counter[str] = Counter()
    source_ids: set[int] = set()
    for e in evidences:
        for s in sources_by_e.get(int(e["id"]), []):
            source_ids.add(int(s["id"]))
            source_counts[str(s["name"])] += 1
    authors = {str(e.get("author")).strip() for e in evidences if e.get("author")}
    raw_engagement = 0.0
    for e in evidences:
        eng = loads(e.get("engagement_json"), {}) or {}
        raw_engagement += sum(_numeric_total(v) for v in eng.values())
    explicit_count = sum(1 for e in evidences if e.get("intent_class") == "explicit_demand")
    probable_count = sum(1 for e in evidences if e.get("intent_class") == "probable_demand")
    comment_demand_count = sum(1 for e in evidences if e.get("content_kind") == "comment")

    timeline: list[dict[str, Any]] = []
    for offset in range(29, -1, -1):
        day = (now - timedelta(days=offset)).date()
        count = sum(1 for dt in dates if dt.date() == day)
        timeline.append({"date": day.isoformat(), "count": count})

    volume_score = min(100.0, math.log1p(current30) / math.log(31) * 100.0) if current30 else 0.0
    if previous7 > 0:
        ratio = current7 / previous7
        growth_score = max(0.0, min(100.0, 50.0 + (ratio - 1.0) * 35.0))
    else:
        growth_score = 70.0 if current7 > 0 else 0.0
    recency_score = min(100.0, (current7 / max(1, current30)) * 100.0) if current30 else 0.0
    diversity_score = min(100.0, len(platforms) * 25.0 + len(source_ids) * 8.0)
    trend_score = round(volume_score * 0.35 + growth_score * 0.30 + recency_score * 0.20 + diversity_score * 0.15, 2)

    timestamp_points = sum(1.0 if e.get("published_time_quality") == "source" else 0.4 if e.get("published_time_quality") == "inherited_thread" else 0.0 for e in evidences)
    timestamp_coverage = (timestamp_points / len(evidences) * 100.0) if evidences else 0.0
    avg_quality = sum(float(e.get("evidence_quality_score") or 0) for e in evidences) / max(1, len(evidences))
    confidence = (
        min(1.0, len(effective) / 12.0) * 35.0
        + min(1.0, len(platforms) / 3.0) * 25.0
        + min(1.0, len(authors) / 10.0) * 20.0
        + min(1.0, timestamp_coverage / 100.0) * 10.0
        + min(1.0, avg_quality / 100.0) * 10.0
    )
    confidence = round(max(0.0, min(100.0, confidence)), 2)
    trusted_trend = round(trend_score * confidence / 100.0, 2)

    payload = {
        "evidence_total": len(evidences),
        "effective_signal_count": len(effective),
        "platform_count": len(platforms),
        "source_count": len(source_ids),
        "unique_authors": len(authors),
        "support_evidence_count": len(supports),
        "support_platform_count": len(support_platforms),
        "explicit_demand_count": explicit_count,
        "probable_demand_count": probable_count,
        "comment_demand_count": comment_demand_count,
        "current_3d": current3,
        "prior_4d": prior4,
        "current_7d": current7,
        "previous_7d": previous7,
        "current_30d": current30,
        "growth_7d_pct": None if growth is None else round(growth, 2),
        "acceleration_pct": None if acceleration is None else round(acceleration, 2),
        "is_new": bool(is_new),
        "raw_engagement_total": round(raw_engagement, 2),
        "data_quality_score": round(avg_quality, 2),
        "timestamp_coverage_pct": round(timestamp_coverage, 2),
        "trend_score": trend_score,
        "trend_confidence": confidence,
        "trusted_trend_score": trusted_trend,
        "timeline": timeline,
        "platform_breakdown": dict(platforms),
        "source_breakdown": dict(source_counts),
        "formula": "trend_score=35% 30d independent volume + 30% week-over-week + 20% recency + 15% source diversity; trusted_trend_score=trend_score*confidence",
        "confidence_formula": "35% independent signals + 25% platform diversity + 20% authors + 10% timestamp coverage + 10% evidence quality",
        "counting_policy": "only explicit/probable demand is counted; identical posts are de-inflated; same wording by different real commenters remains independent demand evidence",
    }
    with conn() as c:
        c.execute(
            """INSERT INTO trend_snapshots(opportunity_id,calculated_at,evidence_total,platform_count,source_count,unique_authors,
               current_7d,previous_7d,current_30d,growth_7d_pct,is_new,raw_engagement_total,trend_score,trend_confidence,
               trusted_trend_score,current_3d,prior_4d,acceleration_pct,effective_signal_count,timestamp_coverage_pct,
               support_evidence_count,support_platform_count,explicit_demand_count,probable_demand_count,comment_demand_count,
               timeline_json,platform_breakdown_json,source_breakdown_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id, now.isoformat(timespec="seconds"), len(evidences), len(platforms), len(source_ids), len(authors),
                current7, previous7, current30, growth, is_new, raw_engagement, trend_score, confidence,
                trusted_trend, current3, prior4, acceleration, len(effective), timestamp_coverage,
                len(supports), len(support_platforms), explicit_count, probable_count, comment_demand_count,
                dumps(timeline), dumps(dict(platforms)), dumps(dict(source_counts)),
            ),
        )
        first_seen = min(dates, default=None)
        last_seen = max(dates, default=None)
        c.execute(
            """UPDATE opportunities SET evidence_count=?,platform_count=?,source_count=?,first_seen_at=?,last_seen_at=?,
               current_7d=?,previous_7d=?,growth_7d_pct=?,trend_score=?,trend_confidence=?,trusted_trend_score=?,
               acceleration_pct=?,effective_signal_count=?,data_quality_score=?,support_evidence_count=?,support_platform_count=?,
               explicit_demand_count=?,probable_demand_count=?,comment_demand_count=?,trend_updated_at=?,updated_at=? WHERE id=?""",
            (
                len(evidences), len(platforms), len(source_ids),
                first_seen.isoformat(timespec="seconds") if first_seen else None,
                last_seen.isoformat(timespec="seconds") if last_seen else None,
                current7, previous7, growth, trend_score, confidence, trusted_trend, acceleration, len(effective), avg_quality,
                len(supports), len(support_platforms), explicit_count, probable_count, comment_demand_count,
                now_iso(), now_iso(), opportunity_id,
            ),
        )
    return payload


def _support_matches(cluster: list[dict[str, Any]], support_rows: list[dict[str, Any]], demand_vectors: dict[int, dict[str, float]], demand_idf: dict[str, float]) -> list[int]:
    if not cluster or not support_rows:
        return []
    cent = _centroid([demand_vectors[int(r["id"])] for r in cluster])
    threshold = max(float(settings.trend_support_similarity_threshold), float(settings.cluster_similarity_threshold))
    matches: list[tuple[float, int]] = []
    for row in support_rows:
        v = _vector(row.get("title") or "", row.get("content") or "", demand_idf)
        sim = _cosine(v, cent)
        if sim >= threshold:
            matches.append((sim, int(row["id"])))
    matches.sort(reverse=True)
    return [eid for _, eid in matches[:120]]


def rebuild_clusters() -> dict[str, Any]:
    started = now_iso()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO cluster_runs(status,method,window_days,threshold,started_at) VALUES(?,?,?,?,?)",
            ("RUNNING", _METHOD, settings.cluster_window_days, settings.cluster_similarity_threshold, started),
        )
        run_id = int(cur.lastrowid)
        # Do not leave disappeared/split historical clusters active in current rankings.
        c.execute("UPDATE opportunities SET cluster_active=0 WHERE cluster_method IN (?,?,?,?,?)", (_METHOD, "real_evidence_tfidf_v4_business_priority", "real_evidence_tfidf_v3", "real_evidence_tfidf_v2", "real_evidence_tfidf_v1"))
    try:
        demand_rows, support_rows = _load_evidence()
        clusters, demand_vectors, demand_idf = _cluster_rows(demand_rows)
        active_ids: list[int] = []
        cross_platform = 0
        support_links = 0
        for cluster in clusters:
            rep = _representative(cluster, demand_vectors)
            signature = _signature(cluster)
            candidate_key = stable_hash("cluster-v5-intent", signature)
            evidence_ids = sorted({int(r["id"]) for r in cluster})
            support_ids = _support_matches(cluster, support_rows, demand_vectors, demand_idf)
            platforms = {r.get("platform") for r in cluster if r.get("platform")}
            if len(platforms) >= 2:
                cross_platform += 1
            with conn() as c:
                oid = _existing_for_cluster(c, evidence_ids, candidate_key)
                now = now_iso()
                if oid is None:
                    cur = c.execute(
                        """INSERT INTO opportunities(title,candidate_key,normalized_need,status,evidence_count,cluster_method,cluster_signature,
                           platform_count,cluster_active,last_cluster_run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (rep.get("title") or signature, candidate_key, rep.get("title") or signature, "NEW", len(evidence_ids), _METHOD, signature, len(platforms), 1, run_id, now, now),
                    )
                    oid = int(cur.lastrowid)
                    old_ids: set[int] = set()
                else:
                    old_ids = {int(r["evidence_id"]) for r in c.execute("SELECT evidence_id FROM opportunity_evidence WHERE opportunity_id=? AND relation_type='demand'", (oid,)).fetchall()}
                    row = c.execute("SELECT analysis_json,status,candidate_key FROM opportunities WHERE id=?", (oid,)).fetchone()
                    changed = old_ids != set(evidence_ids)
                    stale = 1 if changed and row and row["analysis_json"] else 0
                    if changed:
                        c.execute("UPDATE opportunities SET validation_status='STALE' WHERE id=?", (oid,))
                    owner = c.execute("SELECT id FROM opportunities WHERE candidate_key=?", (candidate_key,)).fetchone()
                    safe_key = candidate_key if not owner or int(owner["id"]) == oid else row["candidate_key"]
                    title = rep.get("title") if not (row and row["analysis_json"]) else None
                    if title:
                        c.execute(
                            """UPDATE opportunities SET title=?,candidate_key=?,cluster_method=?,cluster_signature=?,platform_count=?,analysis_stale=?,
                               cluster_active=1,last_cluster_run_id=?,updated_at=? WHERE id=?""",
                            (title, safe_key, _METHOD, signature, len(platforms), stale, run_id, now, oid),
                        )
                    else:
                        c.execute(
                            """UPDATE opportunities SET candidate_key=?,cluster_method=?,cluster_signature=?,platform_count=?,analysis_stale=?,
                               cluster_active=1,last_cluster_run_id=?,updated_at=? WHERE id=?""",
                            (safe_key, _METHOD, signature, len(platforms), stale, run_id, now, oid),
                        )
                    c.execute("DELETE FROM opportunity_evidence WHERE opportunity_id=?", (oid,))
                for eid in evidence_ids:
                    c.execute("INSERT OR IGNORE INTO opportunity_evidence(opportunity_id,evidence_id,relation_type) VALUES(?,?, 'demand')", (oid, eid))
                for eid in support_ids:
                    c.execute("INSERT OR IGNORE INTO opportunity_evidence(opportunity_id,evidence_id,relation_type) VALUES(?,?, 'support')", (oid, eid))
                    support_links += 1
            active_ids.append(oid)
            calculate_trend(oid)

        finished = now_iso()
        with conn() as c:
            c.execute(
                """UPDATE cluster_runs SET status='SUCCESS',finished_at=?,evidence_count=?,cluster_count=?,cross_platform_cluster_count=? WHERE id=?""",
                (finished, len(demand_rows), len(clusters), cross_platform, run_id),
            )
        return {
            "run_id": run_id,
            "status": "SUCCESS",
            "method": _METHOD,
            "processed_evidence": len(demand_rows),
            "support_evidence_considered": len(support_rows),
            "support_links": support_links,
            "candidate_groups": len(clusters),
            "cross_platform_groups": cross_platform,
            "opportunity_ids": active_ids,
            "threshold": settings.cluster_similarity_threshold,
            "support_threshold": settings.trend_support_similarity_threshold,
            "window_days": settings.cluster_window_days,
            "policy": "only explicit_demand/probable_demand evidence from core_demand sources builds clusters; comments can be independent demand evidence; trend_support only supports; supply/showcase/discussion/noise are excluded",
        }
    except Exception as exc:
        with conn() as c:
            c.execute("UPDATE cluster_runs SET status='FAILED',finished_at=?,error=? WHERE id=?", (now_iso(), str(exc), run_id))
        raise


def trend_detail(opportunity_id: int) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute("SELECT * FROM trend_snapshots WHERE opportunity_id=? ORDER BY id DESC LIMIT 1", (opportunity_id,)).fetchone()
    if not row:
        return None
    d = row_to_dict(row) or {}
    d["timeline"] = loads(d.pop("timeline_json", None), [])
    d["platform_breakdown"] = loads(d.pop("platform_breakdown_json", None), {})
    d["source_breakdown"] = loads(d.pop("source_breakdown_json", None), {})
    d["is_new"] = bool(d.get("is_new"))
    d["formula"] = "trend_score=35% 30d independent volume + 30% week-over-week + 20% recency + 15% source diversity"
    d["confidence_formula"] = "35% independent signals + 25% platform diversity + 20% authors + 10% timestamp coverage + 10% evidence quality"
    return d


def trend_ranking(limit: int = 50, cross_source_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM opportunities WHERE cluster_method=? AND cluster_active=1"
    params: list[Any] = [_METHOD]
    if cross_source_only:
        sql += " AND platform_count>=2"
    sql += " ORDER BY coalesce(trusted_trend_score,0) DESC,coalesce(trend_score,0) DESC,current_7d DESC,evidence_count DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
    return [row_to_dict(r) or {} for r in rows]


def latest_cluster_run() -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute("SELECT * FROM cluster_runs ORDER BY id DESC LIMIT 1").fetchone()
    return row_to_dict(row)
