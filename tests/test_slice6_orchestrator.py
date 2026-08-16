"""Ingest-to-distillation orchestration tests with external APIs mocked."""

from __future__ import annotations

import json

import httpx
import respx

from app.models.domain import ProcessState
from app.services.distill_client import DistillClient
from app.services.orchestrator import HEARTBEAT_KEY, run_cycle, run_forever
from app.services.reddit_client import RawPost

BASE_URL = "http://distill.test:8021"
PROCESS_URL = f"{BASE_URL}/v1/process"
JOBS_URL = f"{BASE_URL}/v1/jobs"


class FakeSource:
    def __init__(self, posts, comments=None):
        self._posts = list(posts)
        self._comments = comments or {}

    def new_posts(self, subreddit, limit):
        return list(self._posts[:limit])

    def post_comments(self, post_id, limit):
        return list(self._comments.get(post_id, [])[:limit])


def _post(pid, *, body=("x" * 880), score=5, created=1_700_000_000.0) -> RawPost:
    return RawPost(
        fullname=f"t3_{pid}",
        id=pid,
        title="GME thread",
        body=body,
        author="u",
        score=score,
        permalink=f"/r/wsb/{pid}",
        created_utc=created,
        num_comments=0,
    )


def _job_result(source_item_id: str) -> dict:
    return {
        "status": "ok",
        "request_id": f"req-{source_item_id}",
        "service": "quant-distill-api",
        "source": {
            "source": "quant_reddit",
            "source_type": "reddit",
            "source_item_id": source_item_id,
        },
        "processing": {"model": "llama3.1", "warnings": []},
        "distillation": {"summary": f"summary for {source_item_id}"},
        "sentiment": {"observations": [{"subject": "GME"}]},
        "entities": {"items": [{"ticker": "GME"}]},
    }


def _mock_submit(request: httpx.Request) -> httpx.Response:
    source_item_id = json.loads(request.content)["source_item_id"]
    job_id = f"job-{source_item_id}"
    return httpx.Response(
        202,
        json={
            "status": "accepted",
            "job_id": job_id,
            "job_status": "queued",
            "status_url": f"/v1/jobs/{job_id}",
        },
    )


def _mock_job_succeeded(request: httpx.Request) -> httpx.Response:
    job_id = request.url.path.rsplit("/", 1)[-1]
    source_item_id = job_id.removeprefix("job-")
    return httpx.Response(
        200,
        json={
            "job_id": job_id,
            "endpoint": "/v1/process",
            "status": "succeeded",
            "result": _job_result(source_item_id),
            "error": None,
        },
    )


class TestFullCycle:
    @respx.mock
    def test_cycle_submits_then_completes_on_next_poll(self, repo):
        respx.post(PROCESS_URL).mock(side_effect=_mock_submit)
        respx.get(url__regex=rf"{JOBS_URL}/.*").mock(side_effect=_mock_job_succeeded)
        source = FakeSource(posts=[_post(f"p{i}") for i in range(4)])
        client = DistillClient(base_url=BASE_URL, retries=1)

        first = run_cycle(
            repo,
            reddit_source=source,
            distill_client=client,
            subreddits=["wallstreetbets"],
        )

        # Same cycle: submit new items, then immediately poll the just-created
        # jobs (mock resolves them straight to "succeeded").
        assert first.ingest.posts_new == 4
        assert first.items_submitted == 4
        assert first.items_distilled == 4
        assert first.items_failed == 0
        stored = repo.get_distillation("t3_p0")
        assert stored.request["text"] == "x" * 800
        assert stored.response["distillation"]["summary"] == "summary for t3_p0"
        assert stored.response["sentiment"]["observations"][0]["subject"] == "GME"
        assert stored.response["entities"]["items"][0]["ticker"] == "GME"
        assert repo.get_cursor(HEARTBEAT_KEY) is not None

        second = run_cycle(
            repo,
            reddit_source=source,
            distill_client=client,
            subreddits=["wallstreetbets"],
        )

        assert second.ingest.posts_duplicate == 4
        assert second.items_submitted == 0
        assert second.items_distilled == 0

    @respx.mock
    def test_submission_failure_retries_before_giving_up(self, repo):
        respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(503, json={"status": "error", "detail": "down"})
        )
        source = FakeSource(posts=[_post("failed")])
        client = DistillClient(base_url=BASE_URL, retries=1)

        # First 2 attempts: back to "new" for resubmission, not yet failed.
        for _ in range(2):
            result = run_cycle(
                repo,
                reddit_source=source,
                distill_client=client,
                subreddits=["wallstreetbets"],
            )
            assert result.items_failed == 0
            item = repo.get_item("t3_failed")
            assert item.process_state is ProcessState.new
            assert item.job_id is None

        assert repo.get_item("t3_failed").distill_attempts == 2

        # 3rd attempt reaches the test's max_attempts=3 and is left failed.
        result = run_cycle(
            repo,
            reddit_source=source,
            distill_client=client,
            subreddits=["wallstreetbets"],
            distill_max_attempts=3,
        )

        assert result.items_failed == 1
        assert repo.get_item("t3_failed").process_state is ProcessState.failed
        assert repo.get_distillation("t3_failed") is None

    @respx.mock
    def test_still_running_job_leaves_item_submitted(self, repo):
        respx.post(PROCESS_URL).mock(side_effect=_mock_submit)
        respx.get(url__regex=rf"{JOBS_URL}/.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "job_id": "job-t3_running",
                    "endpoint": "/v1/process",
                    "status": "running",
                    "result": None,
                    "error": None,
                },
            )
        )

        result = run_cycle(
            repo,
            reddit_source=FakeSource(posts=[_post("running")]),
            distill_client=DistillClient(base_url=BASE_URL, retries=1),
            subreddits=["wallstreetbets"],
        )

        assert result.items_submitted == 1
        assert result.items_distilled == 0
        assert result.items_failed == 0
        item = repo.get_item("t3_running")
        assert item.process_state is ProcessState.submitted
        assert item.job_id == "job-t3_running"

    @respx.mock
    def test_job_failed_status_retries_then_gives_up(self, repo):
        respx.post(PROCESS_URL).mock(side_effect=_mock_submit)
        respx.get(url__regex=rf"{JOBS_URL}/.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "job_id": "job-t3_broken",
                    "endpoint": "/v1/process",
                    "status": "failed",
                    "result": None,
                    "error": "DependencyUnavailableError: llm distill call failed",
                },
            )
        )
        source = FakeSource(posts=[_post("broken")])
        client = DistillClient(base_url=BASE_URL, retries=1)

        # Submit + fail once: reset to "new", not yet permanently failed.
        result = run_cycle(
            repo,
            reddit_source=source,
            distill_client=client,
            subreddits=["wallstreetbets"],
            distill_max_attempts=2,
        )
        assert result.items_submitted == 1
        assert result.items_failed == 0
        item = repo.get_item("t3_broken")
        assert item.process_state is ProcessState.new
        assert item.distill_attempts == 1

        # Resubmit + fail again: hits max_attempts=2, left permanently failed.
        result = run_cycle(
            repo,
            reddit_source=source,
            distill_client=client,
            subreddits=["wallstreetbets"],
            distill_max_attempts=2,
        )

        assert result.items_submitted == 1
        assert result.items_failed == 1
        assert repo.get_item("t3_broken").process_state is ProcessState.failed
        assert repo.get_distillation("t3_broken") is None


class TestRunForever:
    @respx.mock
    def test_run_once_drives_a_cycle(self, repo):
        respx.post(PROCESS_URL).mock(side_effect=_mock_submit)
        respx.get(url__regex=rf"{JOBS_URL}/.*").mock(side_effect=_mock_job_succeeded)
        run_forever(
            repo,
            reddit_source=FakeSource(posts=[_post(f"p{i}") for i in range(3)]),
            distill_client=DistillClient(base_url=BASE_URL, retries=1),
            run_once=True,
            subreddits=["wallstreetbets"],
        )

        assert repo.stats()["items_ingested"] == 3
        assert repo.get_cursor(HEARTBEAT_KEY) is not None

