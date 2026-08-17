"""One-time repair utility to requeue stuck/incomplete distillation work.

Two independent repairs, both run unless narrowed with flags:

1. Any ``distillations`` row whose response carries an empty/missing
   ``distillation.summary`` is deleted (it's not useful) and its source item
   is reset to ``new`` for reprocessing.
2. Any ``reddit_items`` row in ``failed``/``submitted`` state (or ``new`` with
   a nonzero ``distill_attempts``, e.g. stuck retries from a quant_distill
   outage) is reset to ``new`` with ``distill_attempts`` zeroed and its job
   fields cleared, so the process worker resubmits it from scratch.

Usage:
    # Dry run (default): prints counts and sample rows only
    python -m scripts.requeue_distillations

    # Apply changes
    python -m scripts.requeue_distillations --apply

    # Only run one of the two repairs
    python -m scripts.requeue_distillations --apply --only empty-summaries
    python -m scripts.requeue_distillations --apply --only stuck-items

    # Wipe all distillations and requeue every item (source text is kept)
    python -m scripts.requeue_distillations --reset-all --apply

    # Delete items too, so posts are re-scraped from Reddit on next ingest
    python -m scripts.requeue_distillations --purge-items --purge-cursors --apply

Requires DATABASE_URL in the environment.
"""

from __future__ import annotations

import argparse

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.db import get_engine
from app.repository.postgres import HEARTBEAT_SOURCE_KEY
from app.repository.schema import distillations, ingest_cursor, reddit_items


def _empty_summary_where_clause():
    # Chained JSON indexing loses the JSONB comparator (and thus .astext)
    # unless the column is explicitly cast to JSONB first.
    response = sa.cast(distillations.c.response, JSONB)
    summary = response["distillation"]["summary"].astext
    return sa.or_(summary.is_(None), summary == "")


def _stuck_items_where_clause():
    return sa.or_(
        reddit_items.c.process_state == "failed",
        reddit_items.c.process_state == "submitted",
        sa.and_(
            reddit_items.c.process_state == "new",
            reddit_items.c.distill_attempts > 0,
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Delete distillations with an empty summary and requeue stuck "
            "reddit_items so the process worker resubmits them."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates (default is dry-run).",
    )
    parser.add_argument(
        "--only",
        choices=["empty-summaries", "stuck-items"],
        default=None,
        help="Run only one repair instead of both.",
    )
    parser.add_argument(
        "--reset-all",
        action="store_true",
        help=(
            "Nuclear option: delete every distillation and reset every item to "
            "'new' so the whole corpus is redistilled. Ignores --only."
        ),
    )
    parser.add_argument(
        "--purge-items",
        action="store_true",
        help=(
            "Delete every distillation AND every reddit_items row, so posts are "
            "re-scraped from Reddit on the next ingest cycle. Use when the stored "
            "title/body themselves are wrong. Ignores --only and --reset-all."
        ),
    )
    parser.add_argument(
        "--purge-cursors",
        action="store_true",
        help="With --purge-items, also clear ingest_cursor watermarks.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=10,
        help="Number of candidate rows to print in dry-run/apply output.",
    )
    return parser.parse_args()


def sample_empty_summary_distillations(conn: sa.Connection, sample_limit: int) -> list[dict]:
    rows = (
        conn.execute(
            sa.select(
                distillations.c.id,
                distillations.c.reddit_fullname,
                distillations.c.request_id,
            )
            .where(_empty_summary_where_clause())
            .order_by(distillations.c.id.desc())
            .limit(max(1, sample_limit))
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def sample_stuck_items(conn: sa.Connection, sample_limit: int) -> list[dict]:
    rows = (
        conn.execute(
            sa.select(
                reddit_items.c.id,
                reddit_items.c.fullname,
                reddit_items.c.process_state,
                reddit_items.c.distill_attempts,
                reddit_items.c.job_id,
            )
            .where(_stuck_items_where_clause())
            .order_by(reddit_items.c.id.desc())
            .limit(max(1, sample_limit))
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def purge_empty_summary_distillations(conn: sa.Connection) -> tuple[int, int]:
    """Delete empty-summary distillations and reset their items to ``new``.

    Returns ``(distillations_deleted, items_reset)``.
    """
    fullnames = conn.execute(
        sa.select(distillations.c.reddit_fullname).where(_empty_summary_where_clause())
    ).scalars().all()
    if not fullnames:
        return 0, 0

    deleted = conn.execute(
        sa.delete(distillations).where(_empty_summary_where_clause())
    ).rowcount or 0
    reset = conn.execute(
        sa.update(reddit_items)
        .where(reddit_items.c.fullname.in_(fullnames))
        .values(
            process_state="new",
            job_id=None,
            distill_request=None,
            distill_attempts=0,
        )
    ).rowcount or 0
    return int(deleted), int(reset)


def requeue_stuck_items(conn: sa.Connection) -> int:
    """Reset stuck ``failed``/``submitted``/retry-stalled items to ``new``."""
    result = conn.execute(
        sa.update(reddit_items)
        .where(_stuck_items_where_clause())
        .values(
            process_state="new",
            job_id=None,
            distill_request=None,
            distill_attempts=0,
        )
    )
    return int(result.rowcount or 0)


def reset_everything(conn: sa.Connection) -> tuple[int, int]:
    """Delete all distillations and reset all items. Returns ``(deleted, reset)``."""
    deleted = conn.execute(sa.delete(distillations)).rowcount or 0
    reset = conn.execute(
        sa.update(reddit_items).values(
            process_state="new",
            job_id=None,
            distill_request=None,
            distill_attempts=0,
        )
    ).rowcount or 0
    return int(deleted), int(reset)


def purge_items(conn: sa.Connection, *, purge_cursors: bool) -> tuple[int, int, int]:
    """Delete all distillations then all items so they are re-ingested.

    Returns ``(distillations_deleted, items_deleted, cursors_deleted)``. The
    heartbeat cursor is preserved so readiness/stats keep reporting liveness.
    """
    deleted_distillations = conn.execute(sa.delete(distillations)).rowcount or 0
    deleted_items = conn.execute(sa.delete(reddit_items)).rowcount or 0
    deleted_cursors = 0
    if purge_cursors:
        deleted_cursors = conn.execute(
            sa.delete(ingest_cursor).where(
                ingest_cursor.c.source_key != HEARTBEAT_SOURCE_KEY
            )
        ).rowcount or 0
    return int(deleted_distillations), int(deleted_items), int(deleted_cursors)


def main() -> int:
    args = parse_args()
    engine = get_engine()

    if args.purge_items:
        with engine.connect() as conn:
            distill_total = conn.execute(
                sa.select(sa.func.count()).select_from(distillations)
            ).scalar_one()
            item_total = conn.execute(
                sa.select(sa.func.count()).select_from(reddit_items)
            ).scalar_one()
        print(
            f"PURGE ITEMS: would delete {distill_total} distillations and "
            f"{item_total} reddit_items (posts will be re-scraped on next ingest)."
        )
        if args.purge_cursors:
            print("  ...and clear ingest_cursor watermarks (heartbeat preserved).")
        if not args.apply:
            print("Dry run only. Re-run with --apply to perform updates.")
            return 0
        with engine.begin() as conn:
            deleted_d, deleted_i, deleted_c = purge_items(
                conn, purge_cursors=args.purge_cursors
            )
        print(
            f"Deleted {deleted_d} distillations, {deleted_i} items, "
            f"{deleted_c} cursors."
        )
        return 0

    if args.reset_all:
        with engine.connect() as conn:
            distill_total = conn.execute(
                sa.select(sa.func.count()).select_from(distillations)
            ).scalar_one()
            item_total = conn.execute(
                sa.select(sa.func.count()).select_from(reddit_items)
            ).scalar_one()
        print(
            f"RESET ALL: would delete {distill_total} distillations and reset "
            f"{item_total} items to 'new'."
        )
        if not args.apply:
            print("Dry run only. Re-run with --apply to perform updates.")
            return 0
        with engine.begin() as conn:
            deleted, reset = reset_everything(conn)
        print(f"Deleted {deleted} distillations; reset {reset} items to 'new'.")
        return 0

    run_empty_summaries = args.only in (None, "empty-summaries")
    run_stuck_items = args.only in (None, "stuck-items")

    with engine.connect() as conn:
        if run_empty_summaries:
            empty_sample = sample_empty_summary_distillations(conn, args.sample_limit)
            print(f"Distillations with an empty summary: {len(empty_sample)}+ (sample below)")
            for row in empty_sample:
                print(f"  id={row['id']} fullname={row['reddit_fullname']} request_id={row['request_id']}")

        if run_stuck_items:
            stuck_sample = sample_stuck_items(conn, args.sample_limit)
            print(f"Stuck reddit_items (failed/submitted/retrying): {len(stuck_sample)}+ (sample below)")
            for row in stuck_sample:
                print(
                    f"  id={row['id']} fullname={row['fullname']} state={row['process_state']} "
                    f"attempts={row['distill_attempts']} job_id={row['job_id']}"
                )

    if not args.apply:
        print("Dry run only. Re-run with --apply to perform updates.")
        return 0

    with engine.begin() as conn:
        if run_empty_summaries:
            deleted, reset = purge_empty_summary_distillations(conn)
            print(f"Deleted {deleted} empty-summary distillations; reset {reset} items to 'new'.")
        if run_stuck_items:
            requeued = requeue_stuck_items(conn)
            print(f"Requeued {requeued} stuck items to 'new'.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
