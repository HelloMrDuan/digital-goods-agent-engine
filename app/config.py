from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")

    app_name: str = "数字商品自动经营系统"
    app_env: str = "production"
    host: str = "0.0.0.0"
    port: int = 8080
    db_path: str = str(BASE_DIR / "data" / "digital_goods.db")

    # Production never falls back to fake AI or fake data.
    qiniu_api_token: str = ""
    qiniu_model: str = "qwen3.5-plus"
    qiniu_base_url: str = ""

    sensenova_api_token: str = ""
    sensenova_model: str = ""
    sensenova_base_url: str = "https://api.sensenova.cn/v1/llm/chat-completions"
    sensenova_input_price_per_million: float = 1.5
    sensenova_output_price_per_million: float = 4.5

    max_ai_items_per_batch: int = 20
    min_evidence_for_analysis: int = 1
    min_score_for_product: int = 70
    high_risk_threshold: int = 80
    daily_ai_budget_cny: float = 5.0

    # V1.2 deterministic commercial validation. This gate runs before paid AI.
    commercial_validation_min_signals: int = 2
    commercial_validation_min_authors: int = 2
    commercial_validation_min_quality: float = 55.0
    commercial_validation_min_timestamp_coverage: float = 50.0
    commercial_validation_min_score: float = 65.0

    github_token: str = ""
    rsshub_base_url: str = ""
    agent_reach_bridge_url: str = ""
    agent_reach_bridge_token: str = ""
    cluster_window_days: int = 60
    trend_window_days: int = 30
    cluster_similarity_threshold: float = 0.34
    cluster_max_evidence: int = 1500
    user_agent: str = "DigitalGoodsRealCollector/1.0 (+local-self-hosted)"

    request_timeout_seconds: float = 25.0
    collector_limit_default: int = 30
    collector_retries: int = 2
    collector_backoff_base_seconds: float = 2.0
    collector_failure_backoff_minutes: int = 15
    collector_failure_backoff_max_minutes: int = 360
    collector_running_stale_minutes: int = 30
    source_stale_after_hours: int = 24
    trend_support_similarity_threshold: float = 0.42
    trend_min_confidence_for_trusted_rank: float = 35.0
    auto_collect_enabled: bool = False
    auto_collect_interval_minutes: int = 360

    @property
    def is_sensenova_configured(self) -> bool:
        return bool(self.sensenova_api_token and self.sensenova_model)

    @property
    def is_qiniu_configured(self) -> bool:
        return bool(self.qiniu_api_token and self.qiniu_base_url and self.qiniu_model)

    @property
    def is_llm_configured(self) -> bool:
        return self.is_qiniu_configured or self.is_sensenova_configured


settings = Settings()
