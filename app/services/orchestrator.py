"""Orchestration worker — ties ``ingest → distill → emit`` into one cycle.

``run_cycle`` performs a single pass:

1. **Ingest** new posts + selective comments (Slice 2).
2. **Distill** each ``new`` item via the LLM (Slice 3).
3. **Emit** one sentiment observation per (item, ticker) (Slice 4) and, after
   per-window aggregation, watchlist-candidate signals per qualifying ticker
   (Slice 5).

``run_forever`` runs cycles on ``POLL_INTERVAL`` with an interruptible sleep and
graceful shutdown on SIGTERM/SIGINT. A per-cycle heartbeat is written to the
``ingest_cursor`` table so readiness/stats can report worker liveness.

Everything is dependency-injected so an end-to-end test can drive a full cycle
with all externals stubbed.
"""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import date

from app.config import settings
from app.models.domain import ProcessState
from app.repository.postgres import HEARTBEAT_SOURCE_KEY, RedditRepository
from app.services.distiller import PROMPT_VERSION, LlmClient, distill_item
from app.services.reddit_client import IngestResult, RedditSource, ingest_once
from app.services.sentiment_emitter import SentimentEmitter
from app.services.signal_emitter import SignalEmitter
from app.timeutil import utcnow

log = logging.getLogger("quant_reddit.orchestrator")

# Re-exported from the repository so callers/tests have one source of truth.
HEARTBEAT_KEY = HEARTBEAT_SOURCE_KEY

# Bound how many freshly-ingested items we distill per cycle (each is an LLM call).
_DEFAULT_DISTILL_LIMIT = 200


@dataclass
class CycleResult:
    ingest: IngestResult
    items_distilled: int = 0
    items_failed: int = 0
    findings: int = 0
    sentiment_emitted: int = 0
    signals_emitted: int = 0

    def as_dict(self) -> dict:
        data = self.__dict__.copy()
        data["ingest"] = self.ingest.as_dict()
        return data


def run_cycle(
    repo: RedditRepository,
    *,
    reddit_source: RedditSource,
    llm_client: LlmClient,
    sentiment_emitter: SentimentEmitter,
    signal_emitter: SignalEmitter,
    subreddit: str | None = None,
    post_batch: int | None = None,
    comments_per_post: int | None = None,
    distill_limit: int = _DEFAULT_DISTILL_LIMIT,
    model: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    day: date | None = None,
) -> CycleResult:
    """Run one full ``ingest → distill → emit`` cycle. Idempotent on re-run."""
    model = model or settings.ollama_model
    day = day or utcnow().date()
    window = day.isoformat()

    ingest_res = ingest_once(
        repo,
        reddit_source,
        subreddit=subreddit,
        post_batch=post_batch,
        comments_per_post=comments_per_post,
    )
    result = CycleResult(ingest=ingest_res)

    new_items = repo.list_items_by_state(ProcessState.new, limit=distill_limit)
    pairs: list[tuple[str, object]] = []

    for item in new_items:
        outcome = distill_item(
            repo, llm_client, item, model=model, prompt_version=prompt_version
        )
        if outcome.status is ProcessState.distilled:
            result.items_distilled += 1
        elif outcome.status is ProcessState.failed:
            result.items_failed += 1

        extraction = outcome.extraction
        if extraction is None:
            continue
        for finding in extraction.extracted:
            result.findings += 1
            rec = sentiment_emitter.emit(
                item,
                finding,
                model=extraction.model,
                prompt_version=extraction.prompt_version,
            )
            if rec is not None:
                result.sentiment_emitted += 1
            pairs.append((item.fullname, finding))

    emitted_signals = signal_emitter.emit_all(
        pairs, model=model, prompt_version=prompt_version, window=window, day=day
    )
    result.signals_emitted = len(emitted_signals)

    repo.set_heartbeat()
    return result


def run_forever(
    repo: RedditRepository,
    *,
    reddit_source: RedditSource,
    llm_client: LlmClient,
    sentiment_emitter: SentimentEmitter,
    signal_emitter: SignalEmitter,
    poll_interval: int | None = None,
    run_once: bool = False,
    **cycle_kwargs,
) -> None:
    """Run cycles until stopped. ``run_once=True`` runs a single cycle (tests)."""
    poll_interval = poll_interval if poll_interval is not None else settings.poll_interval
    stop = threading.Event()

    if not run_once:
        try:
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
        except (ValueError, OSError):
            # Signal handlers can only be installed in the main thread.
            pass

    log.info("orchestrator starting (poll interval %ss)", poll_interval)
    while True:
        try:
            result = run_cycle(
                repo,
                reddit_source=reddit_source,
                llm_client=llm_client,
                sentiment_emitter=sentiment_emitter,
                signal_emitter=signal_emitter,
                **cycle_kwargs,
            )
            log.info("cycle complete: %s", result.as_dict())
        except Exception:  # noqa: BLE001 - a cycle failure must not kill the worker
            log.exception("orchestrator cycle failed")

        if run_once or stop.is_set():
            break
        if stop.wait(timeout=poll_interval):  # interruptible sleep
            break

    log.info("orchestrator stopped")
