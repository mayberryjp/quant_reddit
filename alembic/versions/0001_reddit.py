"""Initial reddit ledger schema.

Creates the ``reddit`` schema and the audit + idempotency ledger tables:
``reddit_items``, ``llm_extractions``, ``emission_log`` and ``ingest_cursor``,
with the unique constraints and indexes described in the spec.

Revision ID: 0001_reddit
Revises:
Create Date: 2026-07-06

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_reddit"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS reddit")

    op.execute(
        """
        CREATE TABLE reddit.reddit_items (
            id               BIGSERIAL PRIMARY KEY,
            fullname         TEXT NOT NULL UNIQUE,
            kind             TEXT NOT NULL,
            subreddit        TEXT NOT NULL,
            author           TEXT,
            title            TEXT,
            body             TEXT NOT NULL DEFAULT '',
            score            INTEGER NOT NULL DEFAULT 0,
            permalink        TEXT,
            parent_fullname  TEXT,
            created_utc      TIMESTAMPTZ NOT NULL,
            fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            process_state    TEXT NOT NULL DEFAULT 'new',
            schema_version   INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_reddit_items_state ON reddit.reddit_items (process_state)"
    )
    op.execute(
        "CREATE INDEX ix_reddit_items_subreddit "
        "ON reddit.reddit_items (subreddit, created_utc DESC)"
    )
    op.execute(
        "CREATE INDEX ix_reddit_items_fetched "
        "ON reddit.reddit_items (fetched_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE reddit.llm_extractions (
            id               BIGSERIAL PRIMARY KEY,
            reddit_fullname  TEXT NOT NULL REFERENCES reddit.reddit_items(fullname),
            model            TEXT NOT NULL,
            prompt_version   TEXT NOT NULL,
            raw_response     JSONB NOT NULL DEFAULT '{}',
            extracted        JSONB NOT NULL DEFAULT '[]',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            schema_version   INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT uq_reddit_extraction
                UNIQUE (reddit_fullname, model, prompt_version)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_reddit_extractions_created "
        "ON reddit.llm_extractions (created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE reddit.emission_log (
            id               BIGSERIAL PRIMARY KEY,
            target           TEXT NOT NULL,
            idempotency_key  TEXT NOT NULL,
            ticker           TEXT,
            request          JSONB NOT NULL DEFAULT '{}',
            status           TEXT NOT NULL,
            http_status      INTEGER,
            response_id      TEXT,
            attempts         INTEGER NOT NULL DEFAULT 1,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_reddit_emission UNIQUE (target, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_reddit_emission_target "
        "ON reddit.emission_log (target, status)"
    )
    op.execute(
        "CREATE INDEX ix_reddit_emission_ticker ON reddit.emission_log (ticker)"
    )
    op.execute(
        "CREATE INDEX ix_reddit_emission_created "
        "ON reddit.emission_log (created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE reddit.ingest_cursor (
            source_key        TEXT PRIMARY KEY,
            last_fullname     TEXT,
            last_created_utc  TIMESTAMPTZ,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reddit.emission_log CASCADE")
    op.execute("DROP TABLE IF EXISTS reddit.llm_extractions CASCADE")
    op.execute("DROP TABLE IF EXISTS reddit.ingest_cursor CASCADE")
    op.execute("DROP TABLE IF EXISTS reddit.reddit_items CASCADE")
    # The schema itself is left in place; it may be shared with sibling services.
