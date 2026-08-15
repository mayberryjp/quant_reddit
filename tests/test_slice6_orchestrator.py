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


def _api_response(source_item_id: str) -> dict:
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


def _mock_success(request: httpx.Request) -> httpx.Response:
    source_item_id = json.loads(request.content)["source_item_id"]
    return httpx.Response(200, json=_api_response(source_item_id))


class TestFullCycle:
    @respx.mock
    def test_cycle_persists_full_response_then_skips_duplicates(self, repo):
        route = respx.post(PROCESS_URL).mock(side_effect=_mock_success)
        source = FakeSource(posts=[_post(f"p{i}") for i in range(4)])
        client = DistillClient(base_url=BASE_URL, retries=1)

        first = run_cycle(
            repo,
            reddit_source=source,
            distill_client=client,
            subreddits=["wallstreetbets"],
        )

        assert first.ingest.posts_new == 4
        assert first.items_distilled == 4
        assert first.items_failed == 0
        assert route.call_count == 4
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
        assert second.items_distilled == 0
        assert route.call_count == 4

    @respx.mock
    def test_api_failure_marks_item_failed(self, repo):
        respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(503, json={"status": "error", "detail": "down"})
        )

        result = run_cycle(
            repo,
            reddit_source=FakeSource(posts=[_post("failed")]),
            distill_client=DistillClient(base_url=BASE_URL, retries=1),
            subreddits=["wallstreetbets"],
        )

        assert result.items_failed == 1
        assert repo.get_item("t3_failed").process_state is ProcessState.failed
        assert repo.get_distillation("t3_failed") is None


class TestRunForever:
    @respx.mock
    def test_run_once_drives_a_cycle(self, repo):
        respx.post(PROCESS_URL).mock(side_effect=_mock_success)
        run_forever(
            repo,
            reddit_source=FakeSource(posts=[_post(f"p{i}") for i in range(3)]),
            distill_client=DistillClient(base_url=BASE_URL, retries=1),
            run_once=True,
            subreddits=["wallstreetbets"],
        )

        assert repo.stats()["items_ingested"] == 3
        assert repo.get_cursor(HEARTBEAT_KEY) is not None
