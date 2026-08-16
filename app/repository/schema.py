"""Authoritative SQLAlchemy Core tables for Reddit ingestion and distillation.

These tables are the single source of truth used by the repository. In production
they map onto the PostgreSQL ``reddit`` schema created by Alembic migration
``0001_reddit``. In tests they are created on SQLite via ``metadata.create_all``
with a schema-translate map, so identical code paths exercise both backends.

Rows are append-mostly and deduplicated by UNIQUE constraints. The only mutations
are source processing-state transitions and ingestion cursor updates.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData(schema="reddit")

# Portable JSON column: JSONB on PostgreSQL, generic JSON elsewhere (SQLite).
JSON_VARIANT = sa.JSON().with_variant(JSONB(), "postgresql")


def _id_column() -> sa.Column:
    # BIGINT on PostgreSQL (BIGSERIAL via Alembic); INTEGER on SQLite so the test
    # backend auto-increments the surrogate primary key.
    return sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


reddit_items = sa.Table(
    "reddit_items",
    metadata,
    _id_column(),
    sa.Column("fullname", sa.Text, nullable=False, unique=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subreddit", sa.Text, nullable=False),
    sa.Column("author", sa.Text, nullable=True),
    sa.Column("title", sa.Text, nullable=True),
    sa.Column("body", sa.Text, nullable=False, server_default=""),
    sa.Column("score", sa.Integer, nullable=False, server_default="0"),
    sa.Column("permalink", sa.Text, nullable=True),
    sa.Column("parent_fullname", sa.Text, nullable=True),
    sa.Column("created_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("process_state", sa.Text, nullable=False, server_default="new"),
    # quant_distill async job (POST /v1/process returns 202 + job_id; poll GET /v1/jobs/{id}).
    sa.Column("job_id", sa.Text, nullable=True),
    sa.Column("distill_request", JSON_VARIANT, nullable=True),
    sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
    sa.Index("ix_reddit_items_state", "process_state"),
    sa.Index("ix_reddit_items_subreddit", "subreddit", "created_utc"),
    sa.Index("ix_reddit_items_fetched", "fetched_at"),
    sa.Index("ix_reddit_items_job_id", "job_id"),
)

distillations = sa.Table(
    "distillations",
    metadata,
    _id_column(),
    sa.Column(
        "reddit_fullname",
        sa.Text,
        sa.ForeignKey(reddit_items.c.fullname),
        nullable=False,
        unique=True,
    ),
    sa.Column("request_id", sa.Text, nullable=False),
    sa.Column("request", JSON_VARIANT, nullable=False),
    sa.Column("response", JSON_VARIANT, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
    sa.Index("ix_reddit_distillations_created", "created_at"),
    sa.Index("ix_reddit_distillations_request_id", "request_id"),
)

ingest_cursor = sa.Table(
    "ingest_cursor",
    metadata,
    sa.Column("source_key", sa.Text, primary_key=True),
    sa.Column("last_fullname", sa.Text, nullable=True),
    sa.Column("last_created_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

cycle_runs = sa.Table(
    "cycle_runs",
    metadata,
    _id_column(),
    sa.Column("run_type", sa.Text, nullable=False),  # "ingest" | "process" | "full"
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("result", JSON_VARIANT, nullable=False),
    sa.Column("error", sa.Text, nullable=True),
    sa.Index("ix_cycle_runs_started", "started_at"),
    sa.Index("ix_cycle_runs_type", "run_type", "started_at"),
)
