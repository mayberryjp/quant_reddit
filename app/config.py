"""Application settings for quant_reddit.

Tunable knobs load from environment variables using the ``QUANT_REDDIT_`` prefix.
``DATABASE_URL`` is read directly (unprefixed) in :mod:`app.db`. External vendor
credentials and sibling-service URLs (Reddit, Ollama, ``quant_signals``,
``quant_sentiment``) are also read unprefixed via explicit validation aliases —
mirroring the sibling ``quant_daily_bars`` service, which reads ``MASSIVE_API_KEY``
unprefixed.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Reddit credentials (read unprefixed) ----------------------------
    reddit_client_id: str = Field(default="", validation_alias="REDDIT_CLIENT_ID")
    reddit_client_secret: str = Field(
        default="", validation_alias="REDDIT_CLIENT_SECRET"
    )
    reddit_user_agent: str = Field(
        default="quant_reddit/0.1 by quant-platform",
        validation_alias="REDDIT_USER_AGENT",
    )
    reddit_username: str | None = Field(
        default=None, validation_alias="REDDIT_USERNAME"
    )
    reddit_password: str | None = Field(
        default=None, validation_alias="REDDIT_PASSWORD"
    )

    # --- Ollama (read unprefixed) ----------------------------------------
    ollama_base_url: str = Field(
        default="http://localhost:11434", validation_alias="OLLAMA_BASE_URL"
    )
    ollama_model: str = Field(default="llama3.1", validation_alias="OLLAMA_MODEL")

    # --- Downstream services (read unprefixed) ---------------------------
    quant_signals_url: str = Field(
        default="http://localhost:8016", validation_alias="QUANT_SIGNALS_URL"
    )
    quant_sentiment_url: str = Field(
        default="http://localhost:8017", validation_alias="QUANT_SENTIMENT_URL"
    )

    # --- Reddit ingestion (QUANT_REDDIT_ prefix) -------------------------
    subreddit: str = "wallstreetbets"
    post_batch: int = 50
    comments_per_post: int = 50
    poll_interval: int = 300

    # --- Emission sources / tuning ---------------------------------------
    signal_source: str = "reddit-wsb-v1"
    sentiment_source: str = "reddit-wsb-v1"
    # Producer reliability weight [0,1] sent on each sentiment observation.
    source_weight: float = 0.5
    min_mentions: int = 3
    watchlist_min_score: float = 0.5
    # A score whose absolute value is <= neutral_band derives to "neutral".
    neutral_band: float = 20.0

    # --- Score bounds ----------------------------------------------------
    score_min: float = -100.0
    score_max: float = 100.0

    # --- Downstream / Ollama HTTP ----------------------------------------
    http_timeout: float = 30.0
    http_retries: int = 3

    # --- Validation limits -----------------------------------------------
    max_reason_length: int = 2000

    # --- Read pagination -------------------------------------------------
    default_page_size: int = 25
    max_page_size: int = 100

    model_config = SettingsConfigDict(env_prefix="QUANT_REDDIT_", extra="ignore")


settings = Settings()
