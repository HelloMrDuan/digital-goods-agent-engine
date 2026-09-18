from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import settings
from ..db import conn, dumps, loads, now_iso, row_to_dict, stable_hash
from .llm import chat_json_with_meta
from .opportunities import get_evidence_for_opportunity, get_opportunity
from .validation import get_validation, validate_opportunity
from .market_intel import market_intel_for_opportunity


AGENT_ORDER = ("signal", "pain", "market", "product", "risk", "judge")

_AGENT_SYSTEM = {
    "signal": """你是 Signal Agent。只依据输入中的真实 evidence digest 识别需求信号，不得编造搜索量、销量、用户量或趋势。输出 JSON 对象：signals(array), signal_strength(0-100), demand_pattern(string), evidence_refs(array of evidence ids), confidence(0-100), reasoning(string)。""",
    "pain": """你是 Pain Agent。根据 Signal Agent 结果和真实 evidence digest 提取用户痛点。输出 JSON 对象：target_users(array), pain_points(array of objects with pain,severity 0-100,evidence_refs), urgency(0-100), confidence(0-100), reasoning(string)。不得补造未出现在证据中的事实。""",
    "market": """你是 Market Agent。根据真实证据、Signal/Pain 结果和输入中的 market_intel 判断数字商品市场可做性。market_intel 中 heat_score 来自真实榜单/互动观测；sales_count 只有 sales_verified=true 时才可引用，并且必须保留 sales_scope（listing/shop/account）的语义，严禁把 shop 销量写成单商品销量。所有评分都是判断，不是市场事实。输出 JSON：category(string), market_score(0-100), purchase_intent(0-100), competition_level(0-100), competition_gaps(array), buyer_profile(array), confidence(0-100), reasoning(string)。不得编造销量、GMV、搜索量或竞品数量。""",
    "product": """你是 Product Agent。把已识别痛点转成可以原创、合法、低售后、可自动交付的数字商品。输出 JSON：recommended_products(array of objects with name,format,why,deliverables), primary_product(string), price_range(object with currency,min,max,reason), production_complexity(0-100), automation_fit(0-100), confidence(0-100), reasoning(string)。禁止推荐盗版、破解、账号、未授权资料包。""",
    "risk": """你是 Risk Agent。审查版权、平台合规、市场饱和、需求真实性、售后和支付意愿风险。输出 JSON：risk_score(0-100), copyright_risk(0-100), saturation_risk(0-100), payment_risk(0-100), platform_risks(array), warnings(array), blockers(array), confidence(0-100), reasoning(string)。高风险必须明确说明。""",
    "judge": """你是 Judge Agent。综合前五个 Agent 和真实证据指标，给出谨慎的商业决策。输出 JSON：final_score(0-100), decision(BUILD|WATCH|DROP), confidence(0-100), title(string), category(string), summary(string), reasoning(string)。不得把 AI 判断包装成事实；证据不足时优先 WATCH 或 DROP。""",
}


class _AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SignalOutput(_AgentOutput):
    signals: list[str]
    signal_strength: float = Field(ge=0, le=100)
    demand_pattern: str
    evidence_refs: list[int] = Field(min_length=1)
    confidence: float = Field(ge=0, le=100)
    reasoning: str


class _PainPoint(_AgentOutput):
    pain: str
    severity: float = Field(ge=0, le=100)
    evidence_refs: list[int] = Field(min_length=1)


class _PainOutput(_AgentOutput):
    target_users: list[str]
    pain_points: list[_PainPoint]
    urgency: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=100)
    reasoning: str


class _MarketOutput(_AgentOutput):
    category: str
    market_score: float = Field(ge=0, le=100)
    purchase_intent: float = Field(ge=0, le=100)
    competition_level: float = Field(ge=0, le=100)
    competition_gaps: list[str]
    buyer_profile: list[str]
    confidence: float = Field(ge=0, le=100)
    reasoning: str


class _RecommendedProduct(_AgentOutput):
    name: str
    format: str
    why: str
    deliverables: list[str]


class _PriceRange(_AgentOutput):
    currency: str
    min: float = Field(ge=0)
    max: float = Field(ge=0)
    reason: str


class _ProductOutput(_AgentOutput):
    recommended_products: list[_RecommendedProduct]
    primary_product: str
    price_range: _PriceRange
    production_complexity: float = Field(ge=0, le=100)
    automation_fit: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=100)
    reasoning: str


class _RiskOutput(_AgentOutput):
    risk_score: float = Field(ge=0, le=100)
    copyright_risk: float = Field(ge=0, le=100)
    saturation_risk: float = Field(ge=0, le=100)
    payment_risk: float = Field(ge=0, le=100)
    platform_risks: list[str]
    warnings: list[str]
    blockers: list[str]
    confidence: float = Field(ge=0, le=100)
    reasoning: str


class _JudgeOutput(_AgentOutput):
    final_score: float = Field(ge=0, le=100)
    decision: Literal["BUILD", "WATCH", "DROP"]
    confidence: float = Field(ge=0, le=100)
    title: str
    category: str
    summary: str
    reasoning: str


_AGENT_SCHEMAS: dict[str, type[_AgentOutput]] = {
    "signal": _SignalOutput,
    "pain": _PainOutput,
    "market": _MarketOutput,
    "product": _ProductOutput,
    "risk": _RiskOutput,
    "judge": _JudgeOutput,
}


def _validate_agent_output(agent_name: str, output: Any, allowed_evidence_ids: set[int]) -> dict[str, Any]:
    schema = _AGENT_SCHEMAS[agent_name]
    try:
        value = schema.model_validate(output).model_dump()
    except ValidationError as exc:
        raise ValueError(
            f"{agent_name} agent output schema invalid: {exc.errors(include_url=False)}"
        ) from exc

    def check_refs(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "evidence_refs":
                    invalid = sorted({int(ref) for ref in child} - allowed_evidence_ids)
                    if invalid:
                        raise ValueError(
                            f"{agent_name} agent evidence_refs outside input evidence: {invalid}"
                        )
                else:
                    check_refs(child)
        elif isinstance(node, list):
            for child in node:
                check_refs(child)

    check_refs(value)
    price = value.get("price_range")
    if isinstance(price, dict) and float(price.get("max") or 0) < float(price.get("min") or 0):
        raise ValueError(f"{agent_name} agent price_range max must be >= min")
    return value


def _score(value: Any, default: float = 0.0) -> float:
    try:
        return round(max(0.0, min(100.0, float(value))), 2)
    except Exception:
        return default


def _ensure_schema() -> None:
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS opportunity_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                category TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                decision TEXT NOT NULL,
                final_score REAL NOT NULL DEFAULT 0,
                market_score REAL NOT NULL DEFAULT 0,
                risk_score REAL NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                purchase_intent REAL NOT NULL DEFAULT 0,
                source_platforms_json TEXT NOT NULL,
                evidence_ids_json TEXT NOT NULL,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                signals_json TEXT,
                target_users_json TEXT,
                pain_points_json TEXT,
                competition_json TEXT,
                market_gaps_json TEXT,
                recommended_products_json TEXT,
                price_range_json TEXT,
                warnings_json TEXT,
                reasoning TEXT,
                provider TEXT,
                model TEXT,
                snapshot_hash TEXT NOT NULL,
                card_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_opportunity_cards_decision_score
                ON opportunity_cards(decision, final_score DESC);

            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                card_id INTEGER,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                input_json TEXT NOT NULL,
                output_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE,
                FOREIGN KEY(card_id) REFERENCES opportunity_cards(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runs_opp_agent
                ON agent_runs(opportunity_id, agent_name, id DESC);
            """
        )


def _task_progress(opportunity_id: int, progress: int, stage: str, message: str) -> None:
    try:
        with conn() as c:
            row = c.execute(
                """SELECT id FROM analysis_tasks
                   WHERE opportunity_id=? AND status IN ('PENDING','RUNNING')
                   ORDER BY id DESC LIMIT 1""",
                (opportunity_id,),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE analysis_tasks SET status='RUNNING',progress=?,stage=?,message=?,updated_at=? WHERE id=?",
                    (progress, stage, message, now_iso(), int(row["id"])),
                )
    except Exception:
        pass


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def build_evidence_digest(opportunity_id: int) -> dict[str, Any]:
    opportunity = get_opportunity(opportunity_id)
    if not opportunity:
        raise KeyError("opportunity not found")
    evidence = get_evidence_for_opportunity(opportunity_id, settings.agent_evidence_limit)
    if len(evidence) < settings.min_evidence_for_analysis:
        raise ValueError(
            f"At least {settings.min_evidence_for_analysis} real evidence item(s) required; found {len(evidence)}"
        )

    platforms = Counter(str(item.get("platform") or "unknown") for item in evidence)
    intents = Counter(str(item.get("intent_class") or "unclassified") for item in evidence)
    authors = {
        f"{item.get('platform')}:{str(item.get('author')).strip().lower()}"
        for item in evidence
        if item.get("author") and str(item.get("author")).strip()
    }
    evidence_ids = [int(item["id"]) for item in evidence if item.get("id") is not None]
    snippets = []
    for item in evidence[: settings.agent_digest_snippets]:
        snippets.append(
            {
                "evidence_id": item.get("id"),
                "platform": item.get("platform"),
                "content_kind": item.get("content_kind"),
                "intent_class": item.get("intent_class"),
                "intent_confidence": item.get("intent_confidence"),
                "quality": item.get("evidence_quality_score"),
                "author": item.get("author"),
                "published_at": item.get("published_at"),
                "title": _compact_text(item.get("title"), 220),
                "content": _compact_text(item.get("content"), settings.agent_snippet_chars),
                "source_url": item.get("source_url"),
            }
        )

    market_intel = market_intel_for_opportunity(opportunity_id)

    snapshot_hash = stable_hash(
        "opportunity-agent-v2-market-intel",
        opportunity.get("cluster_signature"),
        dumps([(item.get("id"), item.get("raw_hash"), item.get("intent_class")) for item in evidence]),
        dumps([
            (
                item.get("id"),
                item.get("heat_score"),
                item.get("sales_count"),
                item.get("sales_scope"),
                item.get("sales_verified"),
                item.get("market_score"),
                item.get("last_seen_at"),
            )
            for item in market_intel.get("items", [])
        ]),
    )
    return {
        "opportunity": {
            "id": opportunity_id,
            "title": opportunity.get("title"),
            "normalized_need": opportunity.get("normalized_need"),
            "cluster_signature": opportunity.get("cluster_signature"),
            "trend_score": opportunity.get("trend_score"),
            "trusted_trend_score": opportunity.get("trusted_trend_score"),
            "data_quality_score": opportunity.get("data_quality_score"),
            "current_7d": opportunity.get("current_7d"),
            "previous_7d": opportunity.get("previous_7d"),
            "growth_7d_pct": opportunity.get("growth_7d_pct"),
        },
        "real_metrics": {
            "evidence_count": len(evidence),
            "evidence_ids": evidence_ids,
            "platform_counts": dict(platforms),
            "unique_authors": len(authors),
            "intent_counts": dict(intents),
        },
        "evidence": snippets,
        "market_intel": market_intel,
        "snapshot_hash": snapshot_hash,
    }


def _record_agent_run(
    *,
    opportunity_id: int,
    agent_name: str,
    status: str,
    input_payload: dict[str, Any],
    output: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    duration_ms: int,
    error: str | None = None,
) -> int:
    meta = meta or {}
    usage = meta.get("usage") or {}
    with conn() as c:
        cur = c.execute(
            """INSERT INTO agent_runs(
                   opportunity_id,agent_name,status,provider,model,prompt_tokens,completion_tokens,total_tokens,
                   duration_ms,input_json,output_json,error,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opportunity_id,
                agent_name,
                status,
                meta.get("provider"),
                meta.get("model"),
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                duration_ms,
                dumps(input_payload),
                dumps(output) if output is not None else None,
                error,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def _run_agent(
    opportunity_id: int,
    agent_name: str,
    payload: dict[str, Any],
    allowed_evidence_ids: set[int],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    started = time.monotonic()
    try:
        output, meta = chat_json_with_meta(
            operation=f"opportunity_agent_{agent_name}",
            entity_type="opportunity",
            entity_id=opportunity_id,
            system=_AGENT_SYSTEM[agent_name],
            payload=payload,
            max_new_tokens=settings.agent_max_new_tokens,
        )
        output = _validate_agent_output(agent_name, output, allowed_evidence_ids)
        duration_ms = int((time.monotonic() - started) * 1000)
        run_id = _record_agent_run(
            opportunity_id=opportunity_id,
            agent_name=agent_name,
            status="SUCCESS",
            input_payload=payload,
            output=output,
            meta=meta,
            duration_ms=duration_ms,
        )
        return output, meta, run_id
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        _record_agent_run(
            opportunity_id=opportunity_id,
            agent_name=agent_name,
            status="FAILED",
            input_payload=payload,
            output=None,
            meta=None,
            duration_ms=duration_ms,
            error=str(exc),
        )
        raise


def _decision(final_score: float, market_score: float, risk_score: float) -> str:
    if risk_score >= settings.high_risk_threshold:
        return "DROP"
    if final_score >= settings.agent_build_score and market_score >= settings.agent_build_market_score:
        return "BUILD"
    if final_score >= settings.agent_watch_score:
        return "WATCH"
    return "DROP"


def run_opportunity_agent_workflow(opportunity_id: int) -> dict[str, Any]:
    _ensure_schema()
    opportunity = get_opportunity(opportunity_id)
    if not opportunity:
        raise KeyError("opportunity not found")
    if not int(opportunity.get("cluster_active") or 0):
        raise ValueError("inactive opportunity cannot be analyzed")

    _task_progress(opportunity_id, 8, "证据摘要", "正在整理真实证据，避免六个 Agent 重复读取全部原文")
    digest = build_evidence_digest(opportunity_id)

    try:
        validation = get_validation(opportunity_id)
        if validation.get("status") in {"NOT_VALIDATED", "STALE"}:
            validation = validate_opportunity(opportunity_id)
    except Exception as exc:
        validation = {"status": "ERROR", "score": 0, "reason_codes": [str(exc)]}

    allowed_evidence_ids = {int(value) for value in digest["real_metrics"]["evidence_ids"]}
    outputs: dict[str, dict[str, Any]] = {}
    run_ids: list[int] = []
    last_meta: dict[str, Any] = {}
    stages = {
        "signal": (20, "Signal Agent", "识别真实需求信号"),
        "pain": (34, "Pain Agent", "提取目标用户与痛点"),
        "market": (49, "Market Agent", "判断市场可做性与竞争缺口"),
        "product": (64, "Product Agent", "生成可原创数字商品方向"),
        "risk": (79, "Risk Agent", "审查版权、平台、饱和和支付风险"),
        "judge": (92, "Judge Agent", "综合评分并形成 BUILD / WATCH / DROP 决策"),
    }

    base_context = {
        "digest": digest,
        "commercial_validation": validation,
        "truth_policy": "Only reason from supplied real evidence and metrics. Never invent market-size facts.",
    }

    for agent_name in AGENT_ORDER:
        progress, stage, message = stages[agent_name]
        _task_progress(opportunity_id, progress, stage, message)
        if agent_name == "signal":
            payload = base_context
        elif agent_name == "pain":
            payload = {"digest": digest, "signal": outputs["signal"]}
        elif agent_name == "market":
            payload = {
                "digest_metrics": digest["real_metrics"],
                "commercial_validation": validation,
                "signal": outputs["signal"],
                "pain": outputs["pain"],
                "market_intel": digest["market_intel"],
            }
        elif agent_name == "product":
            payload = {
                "signal": outputs["signal"],
                "pain": outputs["pain"],
                "market": outputs["market"],
                "evidence_examples": digest["evidence"][:6],
            }
        elif agent_name == "risk":
            payload = {
                "digest_metrics": digest["real_metrics"],
                "market": outputs["market"],
                "product": outputs["product"],
                "evidence_examples": digest["evidence"][:6],
            }
        else:
            payload = {
                "digest_metrics": digest["real_metrics"],
                "commercial_validation": validation,
                "signal": outputs["signal"],
                "pain": outputs["pain"],
                "market": outputs["market"],
                "product": outputs["product"],
                "risk": outputs["risk"],
                "market_intel": digest["market_intel"],
            }
        output, last_meta, run_id = _run_agent(opportunity_id, agent_name, payload, allowed_evidence_ids)
        outputs[agent_name] = output
        run_ids.append(run_id)

    market_score = _score(outputs["market"].get("market_score"))
    purchase_intent = _score(outputs["market"].get("purchase_intent"))
    risk_score = _score(outputs["risk"].get("risk_score"), 50.0)
    final_score = _score(outputs["judge"].get("final_score"))
    confidence = _score(outputs["judge"].get("confidence"))
    deterministic_decision = _decision(final_score, market_score, risk_score)
    proposed = str(outputs["judge"].get("decision") or "").upper()
    decision = deterministic_decision

    source_platforms = sorted(digest["real_metrics"]["platform_counts"].keys())
    recommended_products = outputs["product"].get("recommended_products") or []
    primary_product = outputs["product"].get("primary_product") or (
        recommended_products[0].get("name") if recommended_products and isinstance(recommended_products[0], dict) else None
    )
    category = outputs["judge"].get("category") or outputs["market"].get("category") or opportunity.get("category")
    title = outputs["judge"].get("title") or opportunity.get("title") or "Untitled opportunity"
    reasoning = outputs["judge"].get("reasoning") or outputs["judge"].get("summary") or ""
    card = {
        "opportunity_id": opportunity_id,
        "title": title,
        "category": category,
        "status": "ACTIVE",
        "decision": decision,
        "judge_proposed_decision": proposed or None,
        "final_score": final_score,
        "market_score": market_score,
        "risk_score": risk_score,
        "purchase_intent": purchase_intent,
        "confidence": confidence,
        "source_platforms": source_platforms,
        "evidence_ids": digest["real_metrics"]["evidence_ids"],
        "evidence_count": digest["real_metrics"]["evidence_count"],
        "signals": outputs["signal"].get("signals") or [],
        "target_users": outputs["pain"].get("target_users") or [],
        "pain_points": outputs["pain"].get("pain_points") or [],
        "competition": {
            "level": _score(outputs["market"].get("competition_level"), 50.0),
            "reasoning": outputs["market"].get("reasoning"),
        },
        "market_gaps": outputs["market"].get("competition_gaps") or [],
        "recommended_products": recommended_products,
        "primary_product": primary_product,
        "price_range": outputs["product"].get("price_range") or {},
        "warnings": outputs["risk"].get("warnings") or [],
        "blockers": outputs["risk"].get("blockers") or [],
        "commercial_validation": validation,
        "snapshot_hash": digest["snapshot_hash"],
        "provider": last_meta.get("provider"),
        "model": last_meta.get("model"),
        "reasoning": reasoning,
        "agent_outputs": outputs,
        "generated_at": now_iso(),
    }

    now = now_iso()
    with conn() as c:
        existing = c.execute("SELECT id,created_at FROM opportunity_cards WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        created_at = existing["created_at"] if existing else now
        c.execute(
            """INSERT INTO opportunity_cards(
                   opportunity_id,title,category,status,decision,final_score,market_score,risk_score,confidence,
                   purchase_intent,source_platforms_json,evidence_ids_json,evidence_count,signals_json,target_users_json,
                   pain_points_json,competition_json,market_gaps_json,recommended_products_json,price_range_json,warnings_json,
                   reasoning,provider,model,snapshot_hash,card_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(opportunity_id) DO UPDATE SET
                   title=excluded.title,category=excluded.category,status=excluded.status,decision=excluded.decision,
                   final_score=excluded.final_score,market_score=excluded.market_score,risk_score=excluded.risk_score,
                   confidence=excluded.confidence,purchase_intent=excluded.purchase_intent,
                   source_platforms_json=excluded.source_platforms_json,evidence_ids_json=excluded.evidence_ids_json,
                   evidence_count=excluded.evidence_count,signals_json=excluded.signals_json,target_users_json=excluded.target_users_json,
                   pain_points_json=excluded.pain_points_json,competition_json=excluded.competition_json,
                   market_gaps_json=excluded.market_gaps_json,recommended_products_json=excluded.recommended_products_json,
                   price_range_json=excluded.price_range_json,warnings_json=excluded.warnings_json,reasoning=excluded.reasoning,
                   provider=excluded.provider,model=excluded.model,snapshot_hash=excluded.snapshot_hash,card_json=excluded.card_json,
                   updated_at=excluded.updated_at""",
            (
                opportunity_id,
                title,
                category,
                "ACTIVE",
                decision,
                final_score,
                market_score,
                risk_score,
                confidence,
                purchase_intent,
                dumps(source_platforms),
                dumps(digest["real_metrics"]["evidence_ids"]),
                digest["real_metrics"]["evidence_count"],
                dumps(card["signals"]),
                dumps(card["target_users"]),
                dumps(card["pain_points"]),
                dumps(card["competition"]),
                dumps(card["market_gaps"]),
                dumps(recommended_products),
                dumps(card["price_range"]),
                dumps(card["warnings"]),
                reasoning,
                last_meta.get("provider"),
                last_meta.get("model"),
                digest["snapshot_hash"],
                dumps(card),
                created_at,
                now,
            ),
        )
        card_row = c.execute("SELECT id FROM opportunity_cards WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        card_id = int(card_row["id"])
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            c.execute(f"UPDATE agent_runs SET card_id=? WHERE id IN ({placeholders})", (card_id, *run_ids))
        c.execute(
            """UPDATE opportunities SET category=?,status=?,purchase_intent=?,competition=?,copyright_risk=?,
                      automation_fit=?,overall_score=?,suggested_product=?,ai_reason=?,analysis_json=?,analysis_stale=0,updated_at=?
               WHERE id=?""",
            (
                category,
                "REJECTED_RISK" if risk_score >= settings.high_risk_threshold else "ANALYZED",
                round(purchase_intent),
                round(_score(outputs["market"].get("competition_level"), 50.0)),
                round(_score(outputs["risk"].get("copyright_risk"), risk_score)),
                round(_score(outputs["product"].get("automation_fit"), 50.0)),
                round(final_score),
                primary_product,
                reasoning,
                dumps(card),
                now,
                opportunity_id,
            ),
        )

    _task_progress(opportunity_id, 98, "保存商机卡", f"商机卡已生成：{decision}")
    return {"card_id": card_id, "card": card}


def _decode_card_row(row: Any) -> dict[str, Any]:
    data = row_to_dict(row) or {}
    for key in (
        "source_platforms_json",
        "evidence_ids_json",
        "signals_json",
        "target_users_json",
        "pain_points_json",
        "competition_json",
        "market_gaps_json",
        "recommended_products_json",
        "price_range_json",
        "warnings_json",
        "card_json",
    ):
        if key in data:
            data[key[:-5] if key.endswith("_json") else key] = loads(data.pop(key), {} if key in {"competition_json", "price_range_json", "card_json"} else [])
    return data


def get_opportunity_card(card_id: int) -> dict[str, Any] | None:
    _ensure_schema()
    with conn() as c:
        row = c.execute("SELECT * FROM opportunity_cards WHERE id=?", (card_id,)).fetchone()
    return _decode_card_row(row) if row else None


def get_card_for_opportunity(opportunity_id: int) -> dict[str, Any] | None:
    _ensure_schema()
    with conn() as c:
        row = c.execute("SELECT * FROM opportunity_cards WHERE opportunity_id=?", (opportunity_id,)).fetchone()
    return _decode_card_row(row) if row else None


def list_opportunity_cards(limit: int = 100, decision: str | None = None) -> list[dict[str, Any]]:
    _ensure_schema()
    sql = "SELECT * FROM opportunity_cards"
    params: list[Any] = []
    if decision:
        sql += " WHERE decision=?"
        params.append(str(decision).upper())
    sql += " ORDER BY final_score DESC,id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
    return [_decode_card_row(row) for row in rows]


def list_agent_runs(opportunity_id: int, limit: int = 50) -> list[dict[str, Any]]:
    _ensure_schema()
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM agent_runs WHERE opportunity_id=? ORDER BY id DESC LIMIT ?",
            (opportunity_id, max(1, min(limit, 200))),
        ).fetchall()
    result = []
    for row in rows:
        data = row_to_dict(row) or {}
        data["input"] = loads(data.pop("input_json", None), {})
        data["output"] = loads(data.pop("output_json", None), None)
        result.append(data)
    return result
