"""Application settings for quant_reddit.

Tunable knobs load from environment variables using the ``QUANT_REDDIT_`` prefix.
``DATABASE_URL`` is read directly (unprefixed) in :mod:`app.db`. External vendor
credentials and sibling-service URLs (Reddit, Ollama, ``quant_signals``,
``quant_sentiment``) are also read unprefixed via explicit validation aliases —
mirroring the sibling ``quant_daily_bars`` service, which reads ``MASSIVE_API_KEY``
unprefixed.
"""

from __future__ import annotations

import logging
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Reddit credentials (read unprefixed) ----------------------------
    reddit_client_id: str = Field(default="", validation_alias="REDDIT_CLIENT_ID")
    reddit_client_secret: str = Field(
        default="", validation_alias="REDDIT_CLIENT_SECRET"
    )
    reddit_user_agent: str = Field(
        default="docker:quant_reddit:v0.1.0 (by /u/homelabids)",
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
    # Comma-separated list: QUANT_REDDIT_SUBREDDITS=wallstreetbets,stocks,investing
    subreddits: list[str] = ["wallstreetbets"]
    post_batch: int = 50
    comments_per_post: int = 50
    poll_interval: int = 300
    # Selective comment fetching (owner guidance): only pull a post's comments
    # when it looks high-signal, to stay under Reddit's ~100 req/min budget.
    comment_min_score: int = 50
    comment_min_comments: int = 20

    # --- Emission sources / tuning ---------------------------------------
    signal_source: str = "reddit-wsb-v1"
    watchlist_signal_type: str = "cnbc_mention"
    sentiment_source: str = "reddit-wsb-v1"
    # Producer reliability weight [0,1] sent on each sentiment observation.
    source_weight: float = 0.5

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

_log = logging.getLogger("quant_reddit.config")


def validate_config(s: Settings, database_url: str | None) -> list[str]:
    """Return human-readable configuration problems (never raises).

    Secret *values* are never included — only variable names — so this output is
    safe to log.
    """
    problems: list[str] = []
    if not database_url:
        problems.append(
            "DATABASE_URL is not set (required for persistence and migrations)"
        )
    if not s.reddit_client_id or not s.reddit_client_secret:
        problems.append(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set "
            "(required for Reddit ingestion)"
        )
    for name, url in (
        ("QUANT_SIGNALS_URL", s.quant_signals_url),
        ("QUANT_SENTIMENT_URL", s.quant_sentiment_url),
        ("OLLAMA_BASE_URL", s.ollama_base_url),
    ):
        if not (url.startswith("http://") or url.startswith("https://")):
            problems.append(f"{name} must be an http(s) URL")
    return problems


def log_config_problems() -> list[str]:
    """Validate the active configuration at startup, logging any problems."""
    problems = validate_config(settings, os.environ.get("DATABASE_URL"))
    for problem in problems:
        _log.warning("config validation: %s", problem)
    return problems
