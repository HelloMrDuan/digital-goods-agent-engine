from __future__ import annotations

import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+", re.I)
INTENT_POLICY_VERSION = "intent-v2-buyer-direction"

_BUYER_NEED = re.compile(r"\b(?:i|we)\s+(?:(?:really|urgently|currently|still)\s+)?need\s+(?:a|an|some|help|something)\b")
_TITLE_NEED = re.compile(r"^(?:\[help\]\s*)?(?:need(?:ing)?\s+(?:a|an)|looking for|in search of)\b")
_CHINESE_REQUEST = re.compile(r"^(?:急求|求助|求|想找|需要|有没有)[^。！？\n]{0,28}(?:模板|表格|脚本|工具|教程|资料|excel)")
_CHINESE_COMMISSION = re.compile(r"^(?:求|请|麻烦|帮(?:我)?忙?|有没有人)[^。！？\n]{0,15}(?:做|制作|定制|写)[^。！？\n]{0,20}(?:表格|excel|脚本|模板)")
_SELLER = re.compile(r"\[for hire\]|\b(?:i(?:'m| am)|we(?:'re| are))\s+(?:building|launching|selling|offering|developing)\b|(?:接单|代做|代工|私信领取|承接定制)")


@dataclass(frozen=True)
class IntentDecision:
    label: str
    confidence: float
    demand_weight: float
    reason_codes: tuple[str, ...]


_EXPLICIT = {
    "求一个": 26, "求个": 24, "求一份": 24, "求分享": 26, "求推荐": 24, "求模板": 30, "求教程": 28,
    "求资料": 28, "求工具": 28, "有没有": 22, "有无": 18, "哪里有": 24, "哪里下载": 28,
    "想找": 24, "需要一个": 22, "需要一份": 24, "谁有": 24, "能发一份": 30,
    "可以发一份": 30, "能给吗": 28, "能给我": 28, "可以分享吗": 28, "能分享吗": 28,
    "有适合": 18, "推荐一下": 20, "请问": 10, "求链接": 28, "想要": 14,
    "能不能自动": 24, "有没有办法": 24,
    "looking for": 28, "i'm looking for": 30, "im looking for": 30, "need a": 24, "need an": 24,
    "i need": 22, "any recommendations": 28, "any recommendation": 26, "would anyone": 22,
    "does anyone know": 24, "can i get": 30, "could i get": 30, "may i have": 30,
    "can you share": 30, "can you pm me": 30, "send me a link": 28, "share it please": 28, "share it": 18, "would you mind sharing": 32, "would you be willing to share": 32,
    "i would love a copy": 32, "i'd love a copy": 32, "would also love a copy": 30, "also interested in a copy": 30,
    "can i please get a copy": 32, "could i also get a copy": 32, "can i get a copy": 32,
    "can i have access": 28, "may i have access": 28, "where can i": 20, "trying to find": 22,
    "i'm trying to find": 24, "im trying to find": 24, "if i'm going to buy": 26,
    "if im going to buy": 26, "recommendations?": 18, "share your spreadsheet": 24,
}

_PROBLEM = {
    "怎么": 10, "如何": 10, "需要": 12, "需求": 14, "太麻烦": 18, "麻烦": 8, "不会": 8, "不懂": 8, "卡在": 12,
    "效率低": 14, "希望能": 14, "希望有": 14, "最好能": 12, "自动分类": 18, "自动生成": 14,
    "how do i": 16, "how can i": 16, "too complicated": 20, "overwhelming": 18, "hassle": 18,
    "can't find": 18, "cant find": 18, "doesn't work": 16, "doesnt work": 16, "wish": 10,
    "would like": 12, "i'd like": 12, "id like": 12, "tired of": 12, "help me": 12,
}

_SUPPLY = {
    "免费分享": 28, "模板免费分享": 34, "资料库": 22, "素材库": 22, "合集": 18, "领取": 20,
    "我整理": 22, "我做了": 22, "我做的": 22, "我制作": 22, "分享一下": 18, "分享给大家": 20,
    "教程": 8, "资源网站": 20, "模板资源": 18, "可编辑": 10, "套模板": 14, "套表格": 14,
    "i made": 24, "i've got one": 28, "ive got one": 28, "here it is": 28, "here's": 20,
    "heres": 20, "dm me": 24, "i use": 14, "template file": 20, "free version": 16,
    "for 15% off": 30, "discount": 18, "my template": 18, "i built": 20, "i ended up building": 20,
}

_SHOWCASE = {
    "复盘来啦": 22, "老板都夸": 22, "老板喜欢": 18, "神器绝了": 22, "绝了": 12,
    "动态日历": 12, "超高颜值": 14, "晒一下": 20, "展示": 12, "案例": 8,
    "showcase": 20, "look what i made": 24, "built my own": 20, "my setup": 12,
}

_NOISE_EXACT = {"[deleted]", "[removed]", "", "+1 more replies", "+2 more replies"}


def normalize_text(title: str | None, content: str | None) -> str:
    text = f"{title or ''}\n{content or ''}".lower()
    text = _URL.sub(" ", text)
    text = text.replace("’", "'").replace("‘", "'")
    return _WS.sub(" ", text).strip()


def _score(text: str, table: dict[str, int]) -> tuple[int, list[str]]:
    total = 0
    hits: list[str] = []
    for phrase, weight in table.items():
        if phrase in text:
            total += weight
            hits.append(phrase)
    return total, hits


def classify_intent(title: str | None, content: str | None, content_kind: str = "post") -> IntentDecision:
    text = normalize_text(title, content)
    stripped = text.strip(" >-\t\r\n")
    if stripped in _NOISE_EXACT or re.fullmatch(r"\[?\+\d+ more repl(?:y|ies)\]?", stripped):
        return IntentDecision("noise", 0.99, 0.0, ("placeholder_or_deleted",))
    if len(stripped) < 4:
        return IntentDecision("noise", 0.95, 0.0, ("too_short",))

    if any(p in text for p in ("dm me if you need", "pm me if you need", "message me if you need", "i can send you", "i can share mine", "if u want it just dm me")):
        return IntentDecision("supply", 0.97, 0.0, ("directional_offer",))

    explicit, eh = _score(text, _EXPLICIT)
    problem, ph = _score(text, _PROBLEM)
    title_text = (title or "").strip().lower()
    if title_text.startswith("求"):
        explicit += 14
        eh = ["title_starts_with_求"] + eh
    supply, sh = _score(text, _SUPPLY)
    showcase, vh = _score(text, _SHOWCASE)

    # A descriptive "terms that need a field" is not the author's buyer request.
    if not _BUYER_NEED.search(text) and not _TITLE_NEED.search(title_text):
        for phrase in ("need a", "need an"):
            if phrase in eh:
                explicit -= _EXPLICIT[phrase]
                eh.remove(phrase)
    if _SELLER.search(text):
        return IntentDecision("supply", 0.94, 0.0, ("author_offers_or_builds_product",))
    if _CHINESE_REQUEST.search(title_text) or _CHINESE_COMMISSION.search(title_text):
        explicit = max(30, explicit)
        eh = ["buyer_request_in_title"] + eh

    if "?" in (title or "") + (content or "") or "？" in (title or "") + (content or ""):
        problem += 5
    if content_kind == "comment" and explicit >= 24:
        explicit += 6

    if explicit >= 24 and explicit >= supply + 6 and explicit >= showcase + 6:
        conf = min(0.99, 0.72 + (explicit - max(supply, showcase, problem)) / 100.0)
        reasons = ["explicit_request"] + eh[:4]
        if not str(content or "").strip() and content_kind != "comment":
            conf = min(conf, 0.75)
            reasons.append("title_only_requires_body_verification")
        return IntentDecision("explicit_demand", round(conf, 3), 1.0, tuple(reasons))

    if explicit + problem >= 20 and explicit + problem >= supply + 8 and explicit + problem >= showcase + 8:
        conf = min(0.92, 0.58 + (explicit + problem - max(supply, showcase)) / 120.0)
        return IntentDecision("probable_demand", round(conf, 3), 0.55, tuple(["problem_or_soft_request"] + (eh + ph)[:4]))

    if supply >= 18 and supply >= explicit + problem + 4:
        conf = min(0.98, 0.68 + (supply - explicit - problem) / 100.0)
        return IntentDecision("supply", round(conf, 3), 0.0, tuple(["supply_offer"] + sh[:4]))

    if showcase >= 14 and showcase >= explicit + problem:
        conf = min(0.95, 0.64 + (showcase - explicit - problem) / 100.0)
        return IntentDecision("showcase", round(conf, 3), 0.0, tuple(["showcase"] + vh[:4]))

    if len(stripped) >= 60:
        return IntentDecision("discussion", 0.72, 0.0, ("neutral_discussion",))
    return IntentDecision("noise", 0.70, 0.0, ("weak_or_unclassified",))
