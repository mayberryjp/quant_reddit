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


class DistillationRecord(BaseModel):
    """Exact request and authoritative response for one processed Reddit item."""

    reddit_fullname: str = Field(..., min_length=1)
    request_id: str = Field(..., min_length=1)
    request: dict[str, Any]
    response: dict[str, Any]
    created_at: datetime
    schema_version: int = 1

    @field_validator("created_at", mode="after")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
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
