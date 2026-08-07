"""Add cycle_runs table for ingest/process run history.

Revision ID: 0002_cycle_runs
Revises: 0001_reddit
Create Date: 2026-08-07

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002_cycle_runs"
down_revision: Union[str, None] = "0001_reddit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE reddit.cycle_runs (
            id           BIGSERIAL PRIMARY KEY,
            run_type     TEXT NOT NULL,
            started_at   TIMESTAMPTZ NOT NULL,
            finished_at  TIMESTAMPTZ NOT NULL,
            result       JSONB NOT NULL DEFAULT '{}',
            error        TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_cycle_runs_started ON reddit.cycle_runs (started_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_cycle_runs_type ON reddit.cycle_runs (run_type, started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reddit.cycle_runs CASCADE")
