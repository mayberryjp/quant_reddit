"""Authoritative SQLAlchemy Core table definitions for the reddit audit ledger.

These tables are the single source of truth used by the repository. In production
they map onto the PostgreSQL ``reddit`` schema created by Alembic migration
``0001_reddit``. In tests they are created on SQLite via ``metadata.create_all``
with a schema-translate map, so identical code paths exercise both backends.

The ledger is *append-mostly*: rows are inserted once and deduplicated by UNIQUE
constraints. The only mutations are ``reddit_items.process_state`` transitions and
``emission_log`` attempt bookkeeping.
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
    sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
    sa.Index("ix_reddit_items_state", "process_state"),
    sa.Index("ix_reddit_items_subreddit", "subreddit", "created_utc"),
    sa.Index("ix_reddit_items_fetched", "fetched_at"),
)

llm_extractions = sa.Table(
    "llm_extractions",
    metadata,
    _id_column(),
    sa.Column(
        "reddit_fullname",
        sa.Text,
        sa.ForeignKey(reddit_items.c.fullname),
        nullable=False,
    ),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("raw_response", JSON_VARIANT, nullable=False),
    sa.Column("extracted", JSON_VARIANT, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
    sa.UniqueConstraint(
        "reddit_fullname", "model", "prompt_version", name="uq_reddit_extraction"
    ),
    sa.Index("ix_reddit_extractions_created", "created_at"),
)

emission_log = sa.Table(
    "emission_log",
    metadata,
    _id_column(),
    sa.Column("target", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("ticker", sa.Text, nullable=True),
    sa.Column("request", JSON_VARIANT, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("http_status", sa.Integer, nullable=True),
    sa.Column("response_id", sa.Text, nullable=True),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("target", "idempotency_key", name="uq_reddit_emission"),
    sa.Index("ix_reddit_emission_target", "target", "status"),
    sa.Index("ix_reddit_emission_ticker", "ticker"),
    sa.Index("ix_reddit_emission_created", "created_at"),
)

ingest_cursor = sa.Table(
    "ingest_cursor",
    metadata,
    sa.Column("source_key", sa.Text, primary_key=True),
    sa.Column("last_fullname", sa.Text, nullable=True),
    sa.Column("last_created_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
