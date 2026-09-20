from __future__ import annotations

from typing import Any


DEMAND_TOPICS: tuple[dict[str, Any], ...] = (
    {
        "key": "inventory_spreadsheet",
        "match_groups": (("库存", "inventory", "stock management", "出入库"),),
        "queries": {
            "xiaohongshu": ("求 库存 Excel 表格",),
            "reddit": ("need inventory spreadsheet",),
        },
    },
    {
        "key": "bookkeeping_spreadsheet",
        "match_groups": (("记账", "bookkeeping", "accounting spreadsheet", "expense tracker", "报销", "费用"),),
        "queries": {
            "xiaohongshu": ("求 记账 Excel 表格",),
            "reddit": ("need bookkeeping spreadsheet",),
        },
    },
    {
        "key": "scheduling_spreadsheet",
        "match_groups": (("排班", "轮班", "scheduling spreadsheet", "rota", "shift schedule"),),
        "queries": {
            "xiaohongshu": ("求 排班 Excel 表格",),
            "reddit": ("need scheduling spreadsheet",),
        },
    },
    {
        "key": "payroll_spreadsheet",
        "match_groups": (("工资", "薪资", "薪酬", "payroll", "salary spreadsheet", "wage"),),
        "queries": {
            "xiaohongshu": ("求 工资 Excel 表格",),
            "reddit": ("need payroll spreadsheet",),
        },
    },
    {
        "key": "resume_template",
        "match_groups": (("简历", "resume", "curriculum vitae", "cv template"),),
        "queries": {
            "xiaohongshu": ("求 简历 模板",),
            "reddit": ("need resume template",),
        },
    },
    {
        "key": "invoice_contract_workflow",
        "match_groups": (
            ("invoice", "invoicing", "contract", "agreement", "发票", "账单", "合同"),
            ("automation", "automate", "generator", "catalog", "paperwork", "自动化", "生成", "目录"),
        ),
        "queries": {
            "xiaohongshu": ("求 发票 合同 自动化 表格",),
            "reddit": ("need invoice contract automation",),
        },
    },
    {
        "key": "pdf_excel_ocr",
        "match_groups": (
            ("pdf", "ocr", "扫描件", "识别"),
            ("excel", "spreadsheet", "表格"),
        ),
        "queries": {
            "xiaohongshu": ("求 PDF 转 Excel OCR 工具",),
            "reddit": ("need PDF to Excel OCR tool",),
        },
    },
    {
        "key": "clay_cold_email",
        "match_groups": (
            ("clay",),
            ("cold email", "email sending", "instantly", "lemlist", "smartlead", "邮件自动化", "邮件发送"),
        ),
        "queries": {
            "xiaohongshu": ("求 Clay 邮件 自动化",),
            "reddit": ("need Clay cold email automation",),
        },
    },
)


TOPIC_BY_KEY = {str(topic["key"]): topic for topic in DEMAND_TOPICS}


def topic_matches_text(text: str, topic: dict[str, Any]) -> bool:
    value = str(text or "").lower()
    groups = topic.get("match_groups") or ()
    if not groups:
        return False
    return all(
        any(str(alias).lower() in value for alias in group)
        for group in groups
    )


def detect_topic(title: str | None, content: str | None) -> dict[str, Any] | None:
    text = f"{title or ''} {content or ''}".lower()
    for topic in DEMAND_TOPICS:
        if topic_matches_text(text, topic):
            return topic
    return None


def valid_topic_key(value: Any) -> str | None:
    key = str(value or "").strip()
    return key if key in TOPIC_BY_KEY else None
