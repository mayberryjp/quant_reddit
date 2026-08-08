"""One-time repair utility for bad reddit_items.created_utc values.

Repairs rows where created_utc was persisted as Unix epoch start (1970-01-01)
by setting created_utc = fetched_at.

Usage:
    # Dry run (default): prints counts and sample rows only
    python scripts/backfill_created_utc.py

    # Apply changes
    python scripts/backfill_created_utc.py --apply

Requires DATABASE_URL in the environment.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import sqlalchemy as sa

from app.db import get_engine
from app.repository.schema import reddit_items

EPOCH_START = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPOCH_END = datetime(1970, 1, 2, tzinfo=timezone.utc)


def _candidates_where_clause():
    return sa.and_(
        reddit_items.c.created_utc >= EPOCH_START,
        reddit_items.c.created_utc < EPOCH_END,
        reddit_items.c.fetched_at.is_not(None),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair reddit_items.created_utc values at Unix epoch start by "
            "copying fetched_at."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates (default is dry-run).",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=10,
        help="Number of candidate rows to print in dry-run/apply output.",
    )
    return parser.parse_args()


def sample_candidates(conn: sa.Connection, sample_limit: int) -> list[dict]:
    rows = (
        conn.execute(
            sa.select(
                reddit_items.c.id,
                reddit_items.c.fullname,
                reddit_items.c.subreddit,
                reddit_items.c.created_utc,
                reddit_items.c.fetched_at,
            )
            .where(_candidates_where_clause())
            .order_by(reddit_items.c.fetched_at.desc(), reddit_items.c.id.desc())
            .limit(max(1, sample_limit))
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def candidate_count(conn: sa.Connection) -> int:
    return int(
        conn.execute(
            sa.select(sa.func.count())
            .select_from(reddit_items)
            .where(_candidates_where_clause())
        ).scalar_one()
    )


def apply_backfill(conn: sa.Connection) -> int:
    result = conn.execute(
        sa.update(reddit_items)
        .where(_candidates_where_clause())
        .values(created_utc=reddit_items.c.fetched_at)
    )
    return int(result.rowcount or 0)


def main() -> int:
    args = parse_args()
    engine = get_engine()

    with engine.connect() as conn:
        before = candidate_count(conn)
        sample = sample_candidates(conn, args.sample_limit)

    print(f"Candidates with created_utc at epoch start: {before}")
    if sample:
        print("Sample candidate rows:")
        for row in sample:
            print(
                f"  id={row['id']} fullname={row['fullname']} subreddit={row['subreddit']} "
                f"created_utc={row['created_utc']} fetched_at={row['fetched_at']}"
            )

    if not args.apply:
        print("Dry run only. Re-run with --apply to perform updates.")
        return 0

    with engine.begin() as conn:
        updated = apply_backfill(conn)

    with engine.connect() as conn:
        after = candidate_count(conn)

    print(f"Updated rows: {updated}")
    print(f"Remaining epoch-start rows: {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
