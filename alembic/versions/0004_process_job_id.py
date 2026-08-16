"""Add async quant_distill job tracking columns to reddit_items.

quant_distill's ``POST /v1/process`` now enqueues a job and returns 202 + job_id
instead of the pipeline result. We persist the job id and the exact submitted
request so a later poll can build the authoritative distillations row.

Revision ID: 0004_process_job_id
Revises: 0003_distillations
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_process_job_id"
down_revision: Union[str, None] = "0003_distillations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE reddit.reddit_items ADD COLUMN job_id TEXT")
    op.execute("ALTER TABLE reddit.reddit_items ADD COLUMN distill_request JSONB")
    op.execute(
        "CREATE INDEX ix_reddit_items_job_id ON reddit.reddit_items (job_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS reddit.ix_reddit_items_job_id")
    op.execute("ALTER TABLE reddit.reddit_items DROP COLUMN IF EXISTS distill_request")
    op.execute("ALTER TABLE reddit.reddit_items DROP COLUMN IF EXISTS job_id")
