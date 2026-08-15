"""Application settings for quant_reddit.

Tunable knobs load from environment variables using the ``QUANT_REDDIT_`` prefix.
``DATABASE_URL`` is read directly (unprefixed) in :mod:`app.db`. External vendor
credentials and the shared distillation API URL are also read unprefixed via
explicit validation aliases —
mirroring the sibling ``quant_daily_bars`` service, which reads ``MASSIVE_API_KEY``
unprefixed.
"""

from __future__ import annotations

import logging
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Reddit source mode ----------------------------------------------
    # auto: prefer OAuth/PRAW when credentials are present, otherwise use
    # unauthenticated HTTP JSON scraping of reddit.com endpoints.
    reddit_source_mode: str = Field(default="auto", validation_alias="REDDIT_SOURCE_MODE")

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
    reddit_http_base_url: str = Field(
        default="https://www.reddit.com", validation_alias="REDDIT_HTTP_BASE_URL"
    )

    # --- Shared processing service (read unprefixed) ---------------------
    quant_distill_url: str = Field(
        default="http://localhost:8021", validation_alias="QUANT_DISTILL_URL"
    )

    # --- Reddit ingestion (QUANT_REDDIT_ prefix) -------------------------
    # Comma-separated list: QUANT_REDDIT_SUBREDDITS=wallstreetbets,stocks,investing
    # Kept as a raw string so pydantic-settings does not try to JSON-decode it.
    subreddits_raw: str = Field(
        default="wallstreetbets", validation_alias="QUANT_REDDIT_SUBREDDITS"
    )
    post_batch: int = 50
    comments_per_post: int = 50
    poll_interval: int = 300
    ingest_interval: int = 300
    process_interval: int = 300
    # Selective comment fetching (owner guidance): only pull a post's comments
    # when it looks high-signal, to stay under Reddit's ~100 req/min budget.
    comment_min_score: int = 50
    comment_min_comments: int = 20
    # Post text gating: skip posts whose title+body is shorter than this.
    # For accepted posts, body is truncated to post_max_chars.
    post_min_chars: int = 800
    post_max_chars: int = 800

    # --- Distillation HTTP ------------------------------------------------
    distill_timeout: float = 180.0
    http_retries: int = 3

    # --- Read pagination -------------------------------------------------
    default_page_size: int = 25
    max_page_size: int = 100

    model_config = SettingsConfigDict(env_prefix="QUANT_REDDIT_", extra="ignore")

    @property
    def subreddits(self) -> list[str]:
        return [s.strip() for s in self.subreddits_raw.split(",") if s.strip()]


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
    source_mode = (s.reddit_source_mode or "auto").strip().lower()
    if source_mode not in {"auto", "praw", "scrape"}:
        problems.append("REDDIT_SOURCE_MODE must be one of: auto, praw, scrape")
    if source_mode == "praw" and (not s.reddit_client_id or not s.reddit_client_secret):
        problems.append(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set "
            "(required when REDDIT_SOURCE_MODE=praw)"
        )
    if s.post_min_chars < 0:
        problems.append("QUANT_REDDIT_POST_MIN_CHARS must be >= 0")
    if s.post_max_chars <= 0:
        problems.append("QUANT_REDDIT_POST_MAX_CHARS must be > 0")
    if s.post_max_chars < s.post_min_chars:
        problems.append("QUANT_REDDIT_POST_MAX_CHARS must be >= QUANT_REDDIT_POST_MIN_CHARS")
    for name, url in (
        ("QUANT_DISTILL_URL", s.quant_distill_url),
        ("REDDIT_HTTP_BASE_URL", s.reddit_http_base_url),
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
