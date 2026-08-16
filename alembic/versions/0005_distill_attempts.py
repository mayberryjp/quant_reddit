"""Add a distill attempt counter to reddit_items.

Each submit/job failure increments this counter and resets the item to `new`
for resubmission; once it reaches the configured max, the item is left `failed`.

Revision ID: 0005_distill_attempts
Revises: 0004_process_job_id
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005_distill_attempts"
down_revision: Union[str, None] = "0004_process_job_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE reddit.reddit_items "
        "ADD COLUMN distill_attempts INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reddit.reddit_items DROP COLUMN IF EXISTS distill_attempts")
