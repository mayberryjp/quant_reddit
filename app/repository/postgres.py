"""Repository for the reddit audit + idempotency ledger.

All statements use SQLAlchemy Core expression language (parameterized), so the
same code runs against PostgreSQL (production) and SQLite (tests). The ledger is
append-mostly: inserts are deduplicated by UNIQUE constraints; the only updates
are ``reddit_items.process_state`` transitions and ``emission_log`` bookkeeping.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from app.models.domain import (
    CycleRun,
    EmissionRecord,
    EmissionStatus,
    EmissionTarget,
    IngestCursor,
    LlmExtraction,
    ProcessState,
    RedditItem,
)
from app.repository.schema import (
    cycle_runs,
    emission_log,
    ingest_cursor,
    llm_extractions,
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
        "schema_version": item.schema_version,
    }


def _row_to_item(row) -> RedditItem:
    data = dict(row)
    data.pop("id", None)
    data["created_utc"] = to_utc(data.get("created_utc"))
    data["fetched_at"] = to_utc(data.get("fetched_at"))
    return RedditItem(**data)


def _extraction_to_row(ex: LlmExtraction) -> dict:
    return {
        "reddit_fullname": ex.reddit_fullname,
        "model": ex.model,
        "prompt_version": ex.prompt_version,
        "raw_response": ex.raw_response,
        "extracted": [f.model_dump(mode="json") for f in ex.extracted],
        "created_at": ex.created_at,
        "schema_version": ex.schema_version,
    }


def _row_to_extraction(row) -> LlmExtraction:
    data = dict(row)
    data.pop("id", None)
    data["created_at"] = to_utc(data.get("created_at"))
    return LlmExtraction(**data)


def _row_to_emission(row) -> EmissionRecord:
    data = dict(row)
    data.pop("id", None)
    data["created_at"] = to_utc(data.get("created_at"))
    data["updated_at"] = to_utc(data.get("updated_at"))
    return EmissionRecord(**data)


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

    def list_items_by_state(
        self, state: ProcessState | str, limit: int = 100
    ) -> list[RedditItem]:
        """Return items in a given process state, oldest created first."""
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.select(reddit_items)
                    .where(reddit_items.c.process_state == _val(state))
                    .order_by(reddit_items.c.created_utc.asc(), reddit_items.c.id.asc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [_row_to_item(r) for r in rows]

    # ------------------------------------------------------------------
    # llm_extractions
    # ------------------------------------------------------------------
    def insert_extraction(self, ex: LlmExtraction) -> tuple[LlmExtraction, bool]:
        """Persist an extraction. Dedup by ``(reddit_fullname, model, prompt_version)``."""
        existing = self.get_extraction(ex.reddit_fullname, ex.model, ex.prompt_version)
        if existing is not None:
            return existing, True
        try:
            with self.engine.begin() as conn:
                conn.execute(sa.insert(llm_extractions).values(**_extraction_to_row(ex)))
        except IntegrityError:
            existing = self.get_extraction(
                ex.reddit_fullname, ex.model, ex.prompt_version
            )
            if existing is not None:
                return existing, True
            raise
        return ex, False

    def get_extraction(
        self, reddit_fullname: str, model: str, prompt_version: str
    ) -> LlmExtraction | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(llm_extractions).where(
                        llm_extractions.c.reddit_fullname == reddit_fullname,
                        llm_extractions.c.model == model,
                        llm_extractions.c.prompt_version == prompt_version,
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_extraction(row) if row is not None else None

    # ------------------------------------------------------------------
    # emission_log
    # ------------------------------------------------------------------
    def get_emission(
        self, target: EmissionTarget | str, idempotency_key: str
    ) -> EmissionRecord | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(emission_log).where(
                        emission_log.c.target == _val(target),
                        emission_log.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_emission(row) if row is not None else None

    def record_emission(
        self,
        *,
        target: EmissionTarget | str,
        idempotency_key: str,
        status: EmissionStatus | str,
        ticker: str | None = None,
        request: dict | None = None,
        http_status: int | None = None,
        response_id: str | None = None,
    ) -> EmissionRecord:
        """Insert or update an emission-log row keyed by ``(target, idempotency_key)``.

        The first call inserts with ``attempts = 1``; subsequent calls update the
        outcome and increment ``attempts``.
        """
        now = utcnow()
        request = request or {}
        with self.engine.begin() as conn:
            existing = (
                conn.execute(
                    sa.select(emission_log.c.attempts).where(
                        emission_log.c.target == _val(target),
                        emission_log.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                conn.execute(
                    sa.insert(emission_log).values(
                        target=_val(target),
                        idempotency_key=idempotency_key,
                        ticker=ticker,
                        request=request,
                        status=_val(status),
                        http_status=http_status,
                        response_id=response_id,
                        attempts=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    sa.update(emission_log)
                    .where(
                        emission_log.c.target == _val(target),
                        emission_log.c.idempotency_key == idempotency_key,
                    )
                    .values(
                        ticker=ticker,
                        request=request,
                        status=_val(status),
                        http_status=http_status,
                        response_id=response_id,
                        attempts=existing["attempts"] + 1,
                        updated_at=now,
                    )
                )
        return self.get_emission(target, idempotency_key)

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
        page_size: int = 25,
    ) -> tuple[list[RedditItem], int]:
        conditions: list = []
        if kind:
            conditions.append(reddit_items.c.kind == kind)
        if process_state:
            conditions.append(reddit_items.c.process_state == process_state)
        if subreddit:
            conditions.append(reddit_items.c.subreddit == subreddit)
        where = sa.and_(*conditions) if conditions else sa.true()
        offset = max(page - 1, 0) * page_size
        with self.engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(reddit_items).where(where)
            ).scalar_one()
            rows = (
                conn.execute(
                    sa.select(reddit_items)
                    .where(where)
                    .order_by(reddit_items.c.fetched_at.desc(), reddit_items.c.id.desc())
                    .limit(page_size)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return [_row_to_item(r) for r in rows], int(total)

    def latest_extraction_summaries(
        self, reddit_fullnames: list[str]
    ) -> dict[str, dict]:
        """Return latest extraction ``raw_response`` payloads keyed by fullname.

        When multiple extractions exist for a fullname, the newest by
        ``created_at DESC, id DESC`` is selected.
        """
        if not reddit_fullnames:
            return {}

        ranked = (
            sa.select(
                llm_extractions.c.reddit_fullname,
                llm_extractions.c.raw_response,
                sa.func.row_number()
                .over(
                    partition_by=llm_extractions.c.reddit_fullname,
                    order_by=(
                        llm_extractions.c.created_at.desc(),
                        llm_extractions.c.id.desc(),
                    ),
                )
                .label("rn"),
            )
            .where(llm_extractions.c.reddit_fullname.in_(reddit_fullnames))
            .subquery()
        )

        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.select(ranked.c.reddit_fullname, ranked.c.raw_response).where(
                        ranked.c.rn == 1
                    )
                )
                .mappings()
                .all()
            )

        return {r["reddit_fullname"]: (r["raw_response"] or {}) for r in rows}

    def list_extractions(
        self,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
        reddit_fullname: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[LlmExtraction], int]:
        conditions: list = []
        if model:
            conditions.append(llm_extractions.c.model == model)
        if prompt_version:
            conditions.append(llm_extractions.c.prompt_version == prompt_version)
        if reddit_fullname:
            conditions.append(llm_extractions.c.reddit_fullname == reddit_fullname)
        where = sa.and_(*conditions) if conditions else sa.true()
        offset = max(page - 1, 0) * page_size
        with self.engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(llm_extractions).where(where)
            ).scalar_one()
            rows = (
                conn.execute(
                    sa.select(llm_extractions)
                    .where(where)
                    .order_by(
                        llm_extractions.c.created_at.desc(), llm_extractions.c.id.desc()
                    )
                    .limit(page_size)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return [_row_to_extraction(r) for r in rows], int(total)

    def list_emissions(
        self,
        *,
        target: str | None = None,
        status: str | None = None,
        ticker: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[EmissionRecord], int]:
        conditions: list = []
        if target:
            conditions.append(emission_log.c.target == target)
        if status:
            conditions.append(emission_log.c.status == status)
        if ticker:
            conditions.append(emission_log.c.ticker == ticker)
        where = sa.and_(*conditions) if conditions else sa.true()
        offset = max(page - 1, 0) * page_size
        with self.engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(emission_log).where(where)
            ).scalar_one()
            rows = (
                conn.execute(
                    sa.select(emission_log)
                    .where(where)
                    .order_by(emission_log.c.created_at.desc(), emission_log.c.id.desc())
                    .limit(page_size)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return [_row_to_emission(r) for r in rows], int(total)

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
            extractions_total = conn.execute(
                sa.select(sa.func.count()).select_from(llm_extractions)
            ).scalar_one()
            emission_rows = conn.execute(
                sa.select(
                    emission_log.c.target,
                    emission_log.c.status,
                    sa.func.count(),
                ).group_by(emission_log.c.target, emission_log.c.status)
            ).all()
            last_fetched = conn.execute(
                sa.select(sa.func.max(reddit_items.c.fetched_at))
            ).scalar()

        states = {state: int(n) for state, n in state_rows}
        emissions: dict[str, dict[str, int]] = {"signals": {}, "sentiment": {}}
        for target, status, n in emission_rows:
            emissions.setdefault(target, {})[status] = int(n)

        def _target(name: str) -> dict[str, int]:
            got = emissions.get(name, {})
            return {
                "accepted": got.get("accepted", 0),
                "duplicate": got.get("duplicate", 0),
                "unresolved": got.get("unresolved", 0),
                "failed": got.get("failed", 0),
            }

        return {
            "items_ingested": int(items_total or 0),
            "items_by_state": {
                "new": states.get("new", 0),
                "distilled": states.get("distilled", 0),
                "skipped": states.get("skipped", 0),
                "failed": states.get("failed", 0),
            },
            "extractions": int(extractions_total or 0),
            "emissions": {"signals": _target("signals"), "sentiment": _target("sentiment")},
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
        page_size: int = 25,
    ) -> tuple[list[CycleRun], int]:
        conditions: list = []
        if run_type:
            conditions.append(cycle_runs.c.run_type == run_type)
        where = sa.and_(*conditions) if conditions else sa.true()
        offset = max(page - 1, 0) * page_size
        with self.engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(cycle_runs).where(where)
            ).scalar_one()
            rows = (
                conn.execute(
                    sa.select(cycle_runs)
                    .where(where)
                    .order_by(cycle_runs.c.started_at.desc(), cycle_runs.c.id.desc())
                    .limit(page_size)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return [_row_to_run(r) for r in rows], int(total)
