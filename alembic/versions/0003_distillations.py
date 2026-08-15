"""Add authoritative quant_distill request and response storage.

Revision ID: 0003_distillations
Revises: 0002_cycle_runs
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_distillations"
down_revision: Union[str, None] = "0002_cycle_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE reddit.distillations (
            id               BIGSERIAL PRIMARY KEY,
            reddit_fullname  TEXT NOT NULL UNIQUE
                REFERENCES reddit.reddit_items(fullname),
            request_id       TEXT NOT NULL,
            request          JSONB NOT NULL,
            response         JSONB NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            schema_version   INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_reddit_distillations_created "
        "ON reddit.distillations (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_reddit_distillations_request_id "
        "ON reddit.distillations (request_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reddit.distillations CASCADE")
