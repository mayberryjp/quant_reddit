"""Repository for Reddit source data, distillations, and worker run history.

All statements use SQLAlchemy Core expression language (parameterized), so the
same code runs against PostgreSQL (production) and SQLite (tests). Inserts are
deduplicated by UNIQUE constraints; source processing state and cursors are the
only mutable records.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from app.models.domain import (
    CycleRun,
    DistillationRecord,
    IngestCursor,
    ProcessState,
    RedditItem,
)
from app.repository.schema import (
    cycle_runs,
    distillations,
    ingest_cursor,
    reddit_items,
)
from app.timeutil import to_utc, utcnow


def _val(value):
    """Return the primitive value of an enum, or the value unchanged."""
    return value.value if isinstance(value, enum.Enum) else value


# Worker liveness heartbeat is stored as an ingest_cursor row (no schema change).
HEARTBEAT_SOURCE_KEY = "__worker_heartbeat__"


# ----------------------------------------------------------------------------
# Row <-> model conversion
# ----------------------------------------------------------------------------
def _item_to_row(item: RedditItem) -> dict:
    return {
        "fullname": item.fullname,
        "kind": item.kind.value,
        "subreddit": item.subreddit,
        "author": item.author,
        "title": item.title,
        "body": item.body,
        "score": item.score,
        "permalink": item.permalink,
        "parent_fullname": item.parent_fullname,
        "created_utc": item.created_utc,
        "fetched_at": item.fetched_at,
        "process_state": item.process_state.value,
        "job_id": item.job_id,
        "distill_request": item.distill_request,
        "distill_attempts": item.distill_attempts,
        "schema_version": item.schema_version,
    }


def _row_to_item(row) -> RedditItem:
    data = dict(row)
    data.pop("id", None)
    data["created_utc"] = to_utc(data.get("created_utc"))
    data["fetched_at"] = to_utc(data.get("fetched_at"))
    return RedditItem(**data)


def _distillation_to_row(record: DistillationRecord) -> dict:
    return record.model_dump(mode="python")


def _row_to_distillation(row) -> DistillationRecord:
    data = dict(row)
    data.pop("id", None)
    data["created_at"] = to_utc(data.get("created_at"))
    return DistillationRecord(**data)


def _row_to_cursor(row) -> IngestCursor:
    data = dict(row)
    data["last_created_utc"] = to_utc(data.get("last_created_utc"))
    data["updated_at"] = to_utc(data.get("updated_at"))
    return IngestCursor(**data)


def _row_to_run(row) -> CycleRun:
    data = dict(row)
    data["started_at"] = to_utc(data.get("started_at"))
    data["finished_at"] = to_utc(data.get("finished_at"))
    return CycleRun(**data)


class RedditRepository:
    """Encapsulates every database interaction for the reddit ledger."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # ------------------------------------------------------------------
    # reddit_items
    # ------------------------------------------------------------------
    def insert_item(self, item: RedditItem) -> tuple[RedditItem, bool]:
        """Persist an item. Returns ``(record, is_duplicate)``.

        Dedup is by ``fullname``; a repeated fullname returns the stored record
        unchanged (first write wins).
        """
        existing = self.get_item(item.fullname)
        if existing is not None:
            return existing, True
        try:
            with self.engine.begin() as conn:
                conn.execute(sa.insert(reddit_items).values(**_item_to_row(item)))
        except IntegrityError:
            existing = self.get_item(item.fullname)
            if existing is not None:
                return existing, True
            raise
        return item, False

    def get_item(self, fullname: str) -> RedditItem | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(reddit_items).where(reddit_items.c.fullname == fullname)
                )
                .mappings()
                .first()
            )
        return _row_to_item(row) if row is not None else None

    def set_item_state(self, fullname: str, state: ProcessState | str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.update(reddit_items)
                .where(reddit_items.c.fullname == fullname)
                .values(process_state=_val(state))
            )

    def mark_item_submitted(self, fullname: str, *, job_id: str, request: dict) -> None:
        """Record the quant_distill job id + exact request for a submitted item."""
        with self.engine.begin() as conn:
            conn.execute(
                sa.update(reddit_items)
                .where(reddit_items.c.fullname == fullname)
                .values(
                    process_state=ProcessState.submitted.value,
                    job_id=job_id,
                    distill_request=request,
                )
            )

    def requeue_submitting_items(self) -> int:
        """Recover legacy ``submitting`` rows created by older service versions."""
        with self.engine.begin() as conn:
            result = conn.execute(
                sa.update(reddit_items)
                .where(reddit_items.c.process_state == "submitting")
                .values(process_state=ProcessState.new.value)
            )
        return result.rowcount

    def record_distill_failure(self, fullname: str, *, max_attempts: int) -> ProcessState:
        """Increment the attempt counter for a submit/job failure.

        Resets the item to ``new`` (clearing the stale job) so it is resubmitted
        next cycle, unless ``max_attempts`` has been reached, in which case the
        item is left ``failed``. Returns the resulting state.
        """
        with self.engine.begin() as conn:
            conn.execute(
                sa.update(reddit_items)
                .where(reddit_items.c.fullname == fullname)
                .values(distill_attempts=reddit_items.c.distill_attempts + 1)
            )
            attempts = conn.execute(
                sa.select(reddit_items.c.distill_attempts).where(
                    reddit_items.c.fullname == fullname
                )
            ).scalar_one()
            final_state = (
                ProcessState.failed if attempts >= max_attempts else ProcessState.new
            )
            conn.execute(
                sa.update(reddit_items)
                .where(reddit_items.c.fullname == fullname)
                .values(
                    process_state=final_state.value,
                    job_id=None,
                    distill_request=None,
                )
            )
        return final_state

    def list_items_by_state(
        self, state: ProcessState | str, limit: int | None = 100
    ) -> list[RedditItem]:
        """Return items in a given process state, oldest created first."""
        statement = (
            sa.select(reddit_items)
            .where(reddit_items.c.process_state == _val(state))
            .order_by(reddit_items.c.created_utc.asc(), reddit_items.c.id.asc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as conn:
            rows = (
                conn.execute(statement)
                .mappings()
                .all()
            )
        return [_row_to_item(r) for r in rows]

    # ------------------------------------------------------------------
    # distillations
    # ------------------------------------------------------------------
    def insert_distillation(
        self, record: DistillationRecord
    ) -> tuple[DistillationRecord, bool]:
        """Persist one authoritative API result, deduplicated by source item."""
        existing = self.get_distillation(record.reddit_fullname)
        if existing is not None:
            return existing, True
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    sa.insert(distillations).values(**_distillation_to_row(record))
                )
        except IntegrityError:
            existing = self.get_distillation(record.reddit_fullname)
            if existing is not None:
                return existing, True
            raise
        return record, False

    def get_distillation(self, reddit_fullname: str) -> DistillationRecord | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(distillations).where(
                        distillations.c.reddit_fullname == reddit_fullname
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_distillation(row) if row is not None else None

    # ------------------------------------------------------------------
    # ingest_cursor
    # ------------------------------------------------------------------
    def get_cursor(self, source_key: str) -> IngestCursor | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(ingest_cursor).where(
                        ingest_cursor.c.source_key == source_key
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_cursor(row) if row is not None else None

    def upsert_cursor(
        self,
        source_key: str,
        *,
        last_fullname: str | None = None,
        last_created_utc: datetime | None = None,
    ) -> IngestCursor:
        now = utcnow()
        with self.engine.begin() as conn:
            exists = (
                conn.execute(
                    sa.select(ingest_cursor.c.source_key).where(
                        ingest_cursor.c.source_key == source_key
                    )
                )
                .mappings()
                .first()
            )
            if exists is None:
                conn.execute(
                    sa.insert(ingest_cursor).values(
                        source_key=source_key,
                        last_fullname=last_fullname,
                        last_created_utc=last_created_utc,
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    sa.update(ingest_cursor)
                    .where(ingest_cursor.c.source_key == source_key)
                    .values(
                        last_fullname=last_fullname,
                        last_created_utc=last_created_utc,
                        updated_at=now,
                    )
                )
        return self.get_cursor(source_key)

    # ------------------------------------------------------------------
    # Read / pagination (Slice 7)
    # ------------------------------------------------------------------
    def list_items(
        self,
        *,
        kind: str | None = None,
        process_state: str | None = None,
        subreddit: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[RedditItem], int]:
        conditions: list = []
        if kind:
            conditions.append(reddit_items.c.kind == kind)
        if process_state:
            conditions.append(reddit_items.c.process_state == process_state)
        if subreddit:
            conditions.append(reddit_items.c.subreddit == subreddit)
        where = sa.and_(*conditions) if conditions else sa.true()
        with self.engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(reddit_items).where(where)
            ).scalar_one()
            stmt = (
                sa.select(reddit_items)
                .where(where)
                .order_by(reddit_items.c.fetched_at.desc(), reddit_items.c.id.desc())
            )
            if page_size is not None:
                stmt = stmt.limit(page_size).offset(max(page - 1, 0) * page_size)
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_item(r) for r in rows], int(total)

    def latest_distillation_summaries(
        self, reddit_fullnames: list[str]
    ) -> dict[str, dict]:
        """Return authoritative distillation responses keyed by fullname."""
        if not reddit_fullnames:
            return {}
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.select(distillations.c.reddit_fullname, distillations.c.response)
                    .where(distillations.c.reddit_fullname.in_(reddit_fullnames))
                )
                .mappings()
                .all()
            )

        return {r["reddit_fullname"]: (r["response"] or {}) for r in rows}

    def list_distillations(
        self,
        *,
        request_id: str | None = None,
        reddit_fullname: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[DistillationRecord], int]:
        conditions: list = []
        if request_id:
            conditions.append(distillations.c.request_id == request_id)
        if reddit_fullname:
            conditions.append(distillations.c.reddit_fullname == reddit_fullname)
        where = sa.and_(*conditions) if conditions else sa.true()
        with self.engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(distillations).where(where)
            ).scalar_one()
            stmt = (
                sa.select(distillations)
                .where(where)
                .order_by(distillations.c.created_at.desc(), distillations.c.id.desc())
            )
            if page_size is not None:
                stmt = stmt.limit(page_size).offset(max(page - 1, 0) * page_size)
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_distillation(r) for r in rows], int(total)

    def set_heartbeat(self) -> None:
        """Record a worker liveness heartbeat."""
        self.upsert_cursor(HEARTBEAT_SOURCE_KEY)

    def get_heartbeat(self) -> datetime | None:
        """Return the last worker heartbeat time, or None if never set."""
        cur = self.get_cursor(HEARTBEAT_SOURCE_KEY)
        return cur.updated_at if cur is not None else None

    # ------------------------------------------------------------------
    # Operational
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        """Return True if the database answers a trivial query."""
        try:
            with self.engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - readiness must not raise
            return False

    def stats(self) -> dict:
        """Operational counters computed directly from the ledger."""
        with self.engine.connect() as conn:
            items_total = conn.execute(
                sa.select(sa.func.count()).select_from(reddit_items)
            ).scalar_one()
            state_rows = conn.execute(
                sa.select(reddit_items.c.process_state, sa.func.count()).group_by(
                    reddit_items.c.process_state
                )
            ).all()
            distillations_total = conn.execute(
                sa.select(sa.func.count()).select_from(distillations)
            ).scalar_one()
            last_fetched = conn.execute(
                sa.select(sa.func.max(reddit_items.c.fetched_at))
            ).scalar()

        states = {state: int(n) for state, n in state_rows}
        return {
            "items_ingested": int(items_total or 0),
            "items_by_state": {
                "new": states.get("new", 0),
                "submitted": states.get("submitted", 0),
                "distilled": states.get("distilled", 0),
                "skipped": states.get("skipped", 0),
                "failed": states.get("failed", 0),
            },
            "distillations": int(distillations_total or 0),
            "last_fetched_at": last_fetched,
        }

    # ------------------------------------------------------------------
    # cycle_runs
    # ------------------------------------------------------------------
    def insert_cycle_run(self, run: CycleRun) -> CycleRun:
        """Persist a completed cycle run and return it with its assigned id."""
        with self.engine.begin() as conn:
            result = conn.execute(
                sa.insert(cycle_runs).values(
                    run_type=run.run_type,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    result=run.result,
                    error=run.error,
                )
            )
            row_id = result.inserted_primary_key[0]
        return run.model_copy(update={"id": row_id})

    def list_cycle_runs(
        self,
        *,
        run_type: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[CycleRun], int]:
        conditions: list = []
        if run_type:
            conditions.append(cycle_runs.c.run_type == run_type)
        where = sa.and_(*conditions) if conditions else sa.true()
        with self.engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(cycle_runs).where(where)
            ).scalar_one()
            stmt = (
                sa.select(cycle_runs)
                .where(where)
                .order_by(cycle_runs.c.started_at.desc(), cycle_runs.c.id.desc())
            )
            if page_size is not None:
                stmt = stmt.limit(page_size).offset(max(page - 1, 0) * page_size)
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_run(r) for r in rows], int(total)
