"""Domain models for the reddit audit + idempotency ledger.

All models use Pydantic v2 and mirror the columns of the ``reddit`` schema tables.
Timestamps are normalized to timezone-aware UTC so behaviour is identical across
PostgreSQL (``TIMESTAMPTZ``) and the SQLite test backend (naive datetimes).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RedditKind(str, enum.Enum):
    post = "post"
    comment = "comment"


class ProcessState(str, enum.Enum):
    new = "new"
    distilled = "distilled"
    skipped = "skipped"
    failed = "failed"


class Direction(str, enum.Enum):
    long = "long"
    short = "short"
    neutral = "neutral"


class EmissionTarget(str, enum.Enum):
    signals = "signals"
    sentiment = "sentiment"


class EmissionStatus(str, enum.Enum):
    accepted = "accepted"
    duplicate = "duplicate"
    unresolved = "unresolved"
    failed = "failed"


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RedditItem(BaseModel):
    """A raw ingested Reddit post or comment."""

    fullname: str = Field(..., min_length=1)
    kind: RedditKind
    subreddit: str
    author: str | None = None
    title: str | None = None
    body: str = ""
    score: int = 0
    permalink: str | None = None
    parent_fullname: str | None = None
    created_utc: datetime
    fetched_at: datetime
    process_state: ProcessState = ProcessState.new
    schema_version: int = 1

    @field_validator("created_utc", "fetched_at", mode="after")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


class TickerFinding(BaseModel):
    """One ticker mention distilled from a single Reddit item by the LLM.

    This is the validated shape of each element of ``llm_extractions.extracted``.
    """

    ticker: str = Field(..., min_length=1, max_length=16)
    sentiment_score: float = Field(..., ge=-100.0, le=100.0)
    direction: Direction = Direction.neutral
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_watchlist_candidate: bool = False
    rationale: str = ""
    # cnbc-style pass metadata (optional in reddit flow)
    subject_type: str = "ticker"
    sentiment_label: str | None = None
    horizon: str | None = None
    raw_mention: str | None = None
    company_name: str | None = None
    speaker: str | None = None
    context: str | None = None


class LlmExtraction(BaseModel):
    """Structured LLM output for a single Reddit item."""

    reddit_fullname: str
    model: str
    prompt_version: str
    raw_response: dict[str, Any] = Field(default_factory=dict)
    extracted: list[TickerFinding] = Field(default_factory=list)
    created_at: datetime
    schema_version: int = 1

    @field_validator("created_at", mode="after")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


class EmissionRecord(BaseModel):
    """One downstream POST attempt recorded in ``emission_log``."""

    target: EmissionTarget
    idempotency_key: str
    ticker: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    status: EmissionStatus
    http_status: int | None = None
    response_id: str | None = None
    attempts: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value)


class IngestCursor(BaseModel):
    """Per-source ingestion watermark."""

    source_key: str
    last_fullname: str | None = None
    last_created_utc: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("last_created_utc", "updated_at", mode="after")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value)


class CycleRun(BaseModel):
    """One recorded ingest or process cycle execution."""

    id: int | None = None
    run_type: str  # "ingest" | "process" | "full"
    started_at: datetime
    finished_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @field_validator("started_at", "finished_at", mode="after")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _ensure_utc(value)
