"""Orchestration worker tying Reddit ingestion to the shared distillation API.

``run_cycle`` performs a single pass:

1. **Ingest** new posts + selective comments (Slice 2), then immediately submit
    each ``new`` item to quant_distill's async ``/v1/process`` job queue (the item
    moves to ``submitted``, tagged with the returned ``job_id``).
2. **Poll** each ``submitted`` item's job; on ``succeeded`` persist the exact
   request and authoritative result and move to ``distilled``, on ``failed``
   increment its attempt counter and reset it to ``new`` for resubmission next
   cycle (permanently ``failed`` once ``QUANT_REDDIT_DISTILL_MAX_ATTEMPTS`` is
   reached). Jobs still ``queued``/``running`` are left untouched and are
   re-checked on the next cycle.

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

from app.config import settings
from app.models.domain import CycleRun, DistillationRecord, ProcessState
from app.repository.postgres import HEARTBEAT_SOURCE_KEY, RedditRepository
from app.services.distill_client import DistillApiError, DistillClient
from app.services.reddit_client import IngestResult, RedditSource, ingest_once
from app.timeutil import utcnow

log = logging.getLogger("quant_reddit.orchestrator")

# Re-exported from the repository so callers/tests have one source of truth.
HEARTBEAT_KEY = HEARTBEAT_SOURCE_KEY

# Bound how many freshly-ingested items are submitted per cycle.
_DEFAULT_DISTILL_LIMIT = 200


@dataclass
class CycleResult:
    ingest: IngestResult
    items_submitted: int = 0
    items_distilled: int = 0
    items_failed: int = 0

    def as_dict(self) -> dict:
        data = self.__dict__.copy()
        data["ingest"] = self.ingest.as_dict()
        return data


@dataclass
class ProcessResult:
    items_submitted: int = 0
    items_distilled: int = 0
    items_failed: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def run_ingest_cycle(
    repo: RedditRepository,
    *,
    reddit_source: RedditSource,
    subreddits: list[str] | None = None,
    post_batch: int | None = None,
    comments_per_post: int | None = None,
) -> IngestResult:
    """Run one ingest-only cycle across configured subreddits."""
    active_subreddits = subreddits or settings.subreddits
    merged_ingest = IngestResult()
    for sub in active_subreddits:
        sub_result = ingest_once(
            repo,
            reddit_source,
            subreddit=sub,
            post_batch=post_batch,
            comments_per_post=comments_per_post,
        )
        merged_ingest.posts_new += sub_result.posts_new
        merged_ingest.posts_duplicate += sub_result.posts_duplicate
        merged_ingest.comments_new += sub_result.comments_new
        merged_ingest.comments_duplicate += sub_result.comments_duplicate
        merged_ingest.posts_with_comments += sub_result.posts_with_comments
        merged_ingest.errors += sub_result.errors
    return merged_ingest


def _submit_new_items(
    repo: RedditRepository,
    *,
    distill_client: DistillClient,
    max_attempts: int,
) -> tuple[int, int]:
    """Synchronously submit every pending item. Returns ``(submitted, failed)``."""
    submitted = 0
    failed = 0
    for item in repo.list_items_by_state(ProcessState.new, limit=None):
        try:
            job_id, request = distill_client.submit(item)
            repo.mark_item_submitted(item.fullname, job_id=job_id, request=request)
            submitted += 1
        except DistillApiError:
            log.exception("distill submission failed for %s", item.fullname)
            if repo.record_distill_failure(
                item.fullname, max_attempts=max_attempts
            ) is ProcessState.failed:
                failed += 1
    return submitted, failed


def _poll_submitted_items(
    repo: RedditRepository,
    *,
    distill_client: DistillClient,
    limit: int,
    max_attempts: int,
) -> tuple[int, int]:
    """Poll ``submitted`` items' jobs. Returns ``(distilled, failed)``."""
    distilled = 0
    failed = 0
    for item in repo.list_items_by_state(ProcessState.submitted, limit=limit):
        if not item.job_id:
            log.error("submitted item %s has no job_id", item.fullname)
            if repo.record_distill_failure(
                item.fullname, max_attempts=max_attempts
            ) is ProcessState.failed:
                failed += 1
            continue
        try:
            job = distill_client.get_job(item.job_id)
        except DistillApiError:
            log.warning(
                "polling quant_distill job %s for %s failed; will retry next cycle",
                item.job_id,
                item.fullname,
                exc_info=True,
            )
            continue

        status = job.get("status")
        if status in ("queued", "running"):
            continue
        if status == "succeeded":
            result = job.get("result")
            if not isinstance(result, dict):
                log.error("job %s succeeded but carries no result", item.job_id)
                if repo.record_distill_failure(
                    item.fullname, max_attempts=max_attempts
                ) is ProcessState.failed:
                    failed += 1
                continue
            repo.insert_distillation(
                DistillationRecord(
                    reddit_fullname=item.fullname,
                    request_id=job.get("job_id") or item.job_id,
                    request=item.distill_request or {},
                    response=result,
                    created_at=utcnow(),
                )
            )
            repo.set_item_state(item.fullname, ProcessState.distilled)
            distilled += 1
        else:  # "failed" (or any other terminal/unexpected status)
            log.error(
                "quant_distill job %s failed for %s: %s",
                item.job_id,
                item.fullname,
                job.get("error"),
            )
            if repo.record_distill_failure(
                item.fullname, max_attempts=max_attempts
            ) is ProcessState.failed:
                failed += 1
    return distilled, failed


def run_ingest_and_submit_cycle(
    repo: RedditRepository,
    *,
    reddit_source: RedditSource,
    distill_client: DistillClient,
    subreddits: list[str] | None = None,
    post_batch: int | None = None,
    comments_per_post: int | None = None,
    distill_limit: int = _DEFAULT_DISTILL_LIMIT,
    distill_max_attempts: int | None = None,
) -> CycleResult:
    """Ingest Reddit items and immediately submit pending items for distillation."""
    ingest_result = run_ingest_cycle(
        repo,
        reddit_source=reddit_source,
        subreddits=subreddits,
        post_batch=post_batch,
        comments_per_post=comments_per_post,
    )
    max_attempts = (
        distill_max_attempts
        if distill_max_attempts is not None
        else settings.distill_max_attempts
    )
    requeued = repo.requeue_submitting_items()
    if requeued:
        log.warning("requeued %d legacy submitting items", requeued)
    submitted, failed = _submit_new_items(
        repo,
        distill_client=distill_client,
        max_attempts=max_attempts,
    )
    return CycleResult(
        ingest=ingest_result,
        items_submitted=submitted,
        items_failed=failed,
    )


def run_process_cycle(
    repo: RedditRepository,
    *,
    distill_client: DistillClient,
    distill_limit: int = _DEFAULT_DISTILL_LIMIT,
    distill_max_attempts: int | None = None,
) -> ProcessResult:
    """Run one process-only cycle: poll ``submitted`` jobs for completion."""
    max_attempts = (
        distill_max_attempts
        if distill_max_attempts is not None
        else settings.distill_max_attempts
    )
    result = ProcessResult()
    result.items_distilled, poll_failed = _poll_submitted_items(
        repo, distill_client=distill_client, limit=distill_limit, max_attempts=max_attempts
    )
    result.items_failed = poll_failed
    return result


def run_cycle(
    repo: RedditRepository,
    *,
    reddit_source: RedditSource,
    distill_client: DistillClient,
    subreddits: list[str] | None = None,
    post_batch: int | None = None,
    comments_per_post: int | None = None,
    distill_limit: int = _DEFAULT_DISTILL_LIMIT,
    distill_max_attempts: int | None = None,
) -> CycleResult:
    """Run one full ``ingest -> submit -> poll`` cycle. Idempotent on re-run."""
    result = run_ingest_and_submit_cycle(
        repo,
        reddit_source=reddit_source,
        distill_client=distill_client,
        subreddits=subreddits,
        post_batch=post_batch,
        comments_per_post=comments_per_post,
        distill_limit=distill_limit,
        distill_max_attempts=distill_max_attempts,
    )
    process_result = run_process_cycle(
        repo,
        distill_client=distill_client,
        distill_limit=distill_limit,
        distill_max_attempts=distill_max_attempts,
    )

    result.items_distilled = process_result.items_distilled
    result.items_failed += process_result.items_failed

    repo.set_heartbeat()
    return result


def _install_stop_handlers(stop: threading.Event) -> None:
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    except (ValueError, OSError):
        # Signal handlers can only be installed in the main thread.
        pass


def run_ingest_forever(
    repo: RedditRepository,
    *,
    reddit_source: RedditSource,
    distill_client: DistillClient,
    poll_interval: int | None = None,
    run_once: bool = False,
    **ingest_kwargs,
) -> None:
    """Run ingest-and-submit cycles until stopped."""
    poll_interval = poll_interval if poll_interval is not None else settings.poll_interval
    stop = threading.Event()

    if not run_once:
        _install_stop_handlers(stop)

    log.info("ingest worker starting (poll interval %ss)", poll_interval)
    while True:
        started = utcnow()
        error: str | None = None
        try:
            ingest_result = run_ingest_and_submit_cycle(
                repo,
                reddit_source=reddit_source,
                distill_client=distill_client,
                **ingest_kwargs,
            )
            log.info("ingest cycle complete: %s", ingest_result.as_dict())
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest cycle failed")
            error = str(exc)
            ingest_result = IngestResult()
        try:
            repo.insert_cycle_run(
                CycleRun(
                    run_type="ingest",
                    started_at=started,
                    finished_at=utcnow(),
                    result=ingest_result.as_dict(),
                    error=error,
                )
            )
        except Exception:  # noqa: BLE001
            log.warning("failed to persist ingest cycle run", exc_info=True)

        if run_once or stop.is_set():
            break
        if stop.wait(timeout=poll_interval):
            break

    log.info("ingest worker stopped")


def run_process_forever(
    repo: RedditRepository,
    *,
    distill_client: DistillClient,
    poll_interval: int | None = None,
    run_once: bool = False,
    **process_kwargs,
) -> None:
    """Run process-only cycles until stopped."""
    poll_interval = poll_interval if poll_interval is not None else settings.poll_interval
    stop = threading.Event()

    if not run_once:
        _install_stop_handlers(stop)

    log.info("process worker starting (poll interval %ss)", poll_interval)
    while True:
        started = utcnow()
        error: str | None = None
        try:
            process_result = run_process_cycle(
                repo,
                distill_client=distill_client,
                **process_kwargs,
            )
            log.info("process cycle complete: %s", process_result.as_dict())
            repo.set_heartbeat()
        except Exception as exc:  # noqa: BLE001
            log.exception("process cycle failed")
            error = str(exc)
            process_result = ProcessResult()
        try:
            repo.insert_cycle_run(
                CycleRun(
                    run_type="process",
                    started_at=started,
                    finished_at=utcnow(),
                    result=process_result.as_dict(),
                    error=error,
                )
            )
        except Exception:  # noqa: BLE001
            log.warning("failed to persist process cycle run", exc_info=True)

        if run_once or stop.is_set():
            break
        if stop.wait(timeout=poll_interval):
            break

    log.info("process worker stopped")


def run_forever(
    repo: RedditRepository,
    *,
    reddit_source: RedditSource,
    distill_client: DistillClient,
    poll_interval: int | None = None,
    run_once: bool = False,
    **cycle_kwargs,
) -> None:
    """Run cycles until stopped. ``run_once=True`` runs a single cycle (tests)."""
    poll_interval = poll_interval if poll_interval is not None else settings.poll_interval
    stop = threading.Event()

    if not run_once:
        _install_stop_handlers(stop)

    log.info("orchestrator starting (poll interval %ss)", poll_interval)
    while True:
        try:
            result = run_cycle(
                repo,
                reddit_source=reddit_source,
                distill_client=distill_client,
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
