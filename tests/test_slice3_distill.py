"""Shared quant_distill async job API client contract tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.services.distill_client import DistillApiError, DistillClient

BASE_URL = "http://distill.test:8021"
PROCESS_URL = f"{BASE_URL}/v1/process"
JOBS_URL = f"{BASE_URL}/v1/jobs"


def _accepted(job_id: str = "job-1") -> dict:
    return {
        "status": "accepted",
        "job_id": job_id,
        "job_status": "queued",
        "status_url": f"/v1/jobs/{job_id}",
    }


def _job_result() -> dict:
    return {
        "status": "ok",
        "request_id": "req-1",
        "service": "quant-distill-api",
        "source": {
            "source": "quant_reddit",
            "source_type": "reddit",
            "source_item_id": "t3_abc123",
        },
        "processing": {"model": "llama3.1", "warnings": []},
        "distillation": {"summary": "A concise summary", "key_topics": ["GME"]},
        "sentiment": {"observations": [{"subject": "GME"}]},
        "entities": {"items": [{"ticker": "GME"}]},
    }


def _job(job_id: str = "job-1", *, status: str = "succeeded", result=None, error=None) -> dict:
    return {
        "job_id": job_id,
        "endpoint": "/v1/process",
        "status": status,
        "result": result,
        "error": error,
    }


class TestSubmit:
    @respx.mock
    def test_submit_maps_reddit_item_and_returns_job_id(self, make_item):
        route = respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(202, json=_accepted())
        )
        item = make_item(fullname="t3_abc123", title="GME thread", body="raw body")

        job_id, request = DistillClient(base_url=BASE_URL, retries=1).submit(item)

        sent = json.loads(route.calls[0].request.content)
        assert sent["source"] == "quant_reddit"
        assert sent["source_type"] == "reddit"
        assert sent["source_item_id"] == "t3_abc123"
        assert sent["title"] == "GME thread"
        assert sent["text"] == "raw body"
        assert sent["metadata"]["subreddit"] == "wallstreetbets"
        assert request == sent
        assert job_id == "job-1"

    @respx.mock
    def test_retries_503_then_succeeds(self, make_item):
        route = respx.post(PROCESS_URL).mock(
            side_effect=[
                httpx.Response(503, json={"status": "error", "detail": "unavailable"}),
                httpx.Response(202, json=_accepted()),
            ]
        )

        job_id, _ = DistillClient(base_url=BASE_URL, retries=2, backoff=0).submit(
            make_item()
        )

        assert job_id == "job-1"
        assert route.call_count == 2

    @respx.mock
    def test_does_not_retry_422(self, make_item):
        route = respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(
                422,
                json={"status": "error", "detail": "field 'text' must not be empty"},
            )
        )

        with pytest.raises(DistillApiError, match="HTTP 422"):
            DistillClient(base_url=BASE_URL, retries=3, backoff=0).submit(make_item())

        assert route.call_count == 1

    @respx.mock
    def test_does_not_retry_413(self, make_item):
        route = respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(413, json={"status": "error", "detail": "too large"})
        )

        with pytest.raises(DistillApiError, match="HTTP 413"):
            DistillClient(base_url=BASE_URL, retries=3, backoff=0).submit(make_item())

        assert route.call_count == 1

    @respx.mock
    def test_rejects_response_missing_job_id(self, make_item):
        respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(202, json={"status": "accepted"})
        )

        with pytest.raises(DistillApiError, match="job_id"):
            DistillClient(base_url=BASE_URL, retries=1).submit(make_item())


class TestGetJob:
    @respx.mock
    def test_get_job_succeeded_returns_result(self):
        respx.get(f"{JOBS_URL}/job-1").mock(
            return_value=httpx.Response(200, json=_job(status="succeeded", result=_job_result()))
        )

        job = DistillClient(base_url=BASE_URL, retries=1).get_job("job-1")

        assert job["status"] == "succeeded"
        assert job["result"]["distillation"]["summary"] == "A concise summary"

    @respx.mock
    def test_get_job_queued(self):
        respx.get(f"{JOBS_URL}/job-1").mock(
            return_value=httpx.Response(200, json=_job(status="queued"))
        )

        job = DistillClient(base_url=BASE_URL, retries=1).get_job("job-1")

        assert job["status"] == "queued"
        assert job["result"] is None

    @respx.mock
    def test_get_job_failed(self):
        respx.get(f"{JOBS_URL}/job-1").mock(
            return_value=httpx.Response(
                200, json=_job(status="failed", error="ReadTimeout")
            )
        )

        job = DistillClient(base_url=BASE_URL, retries=1).get_job("job-1")

        assert job["status"] == "failed"
        assert job["error"] == "ReadTimeout"

    @respx.mock
    def test_get_job_not_found(self):
        respx.get(f"{JOBS_URL}/missing").mock(
            return_value=httpx.Response(
                404, json={"status": "error", "code": "not_found", "error": "job not found"}
            )
        )

        with pytest.raises(DistillApiError, match="not found"):
            DistillClient(base_url=BASE_URL, retries=1).get_job("missing")

