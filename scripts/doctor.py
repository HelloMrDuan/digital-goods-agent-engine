from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collectors import agent_reach_doctor, rsshub_doctor
from app.config import settings
from app.db import init_db
from app.services.ingest import backfill_intents
from app.services.clustering import latest_cluster_run
from app.services.scheduler import scheduler_status


def main() -> None:
    init_db()
    intent_backfill = backfill_intents()
    print(json.dumps({
        "mode": "REAL_DATA_ONLY",
        "mock_data": False,
        "mock_ai": False,
        "sensenova_configured": settings.is_sensenova_configured,
        "sensenova_model": settings.sensenova_model or None,
        "db_path": settings.db_path,
        "collectors": {
            "github_official_api": True,
            "v2ex_auxiliary_only": True,
            "google_news_rss": True,
            "bilibili_direct_search": True,
            "youtube_ytdlp_installed": __import__("shutil").which("yt-dlp") is not None,
            "rsshub": rsshub_doctor(),
            "agent_reach": agent_reach_doctor(),
            "opencli_installed": __import__("shutil").which("opencli") is not None,
        },
        "content_intent_gate": {
            "enabled": True,
            "eligible": ["explicit_demand", "probable_demand"],
            "excluded": ["supply", "showcase", "discussion", "noise"],
            "comment_evidence": True,
            "intent_backfill": intent_backfill,
        },
        "business_priority": {
            "P0": "core_demand: real posts/comments must pass explicit/probable intent gate",
            "P1": "trend_support: trend evidence only",
            "P2": "supply_intel: supply/open-source intelligence only",
            "P3": "auxiliary: excluded from business-demand clustering",
            "v2ex_default": "P3 disabled/not seeded on new bootstrap",
        },
        "clustering": {
            "method": "real_evidence_tfidf_v5_content_intent_gate",
            "window_days": settings.cluster_window_days,
            "trend_window_days": settings.trend_window_days,
            "similarity_threshold": settings.cluster_similarity_threshold,
            "support_similarity_threshold": settings.trend_support_similarity_threshold,
            "semantic_aliases": str(ROOT / "config" / "semantic_aliases.json"),
            "max_evidence": settings.cluster_max_evidence,
            "latest_run": latest_cluster_run(),
        },
        "scheduler": scheduler_status(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
