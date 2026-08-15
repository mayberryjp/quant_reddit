"""Shared quant_distill API client contract tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.services.distill_client import DistillApiError, DistillClient

BASE_URL = "http://distill.test:8021"
PROCESS_URL = f"{BASE_URL}/v1/process"


def _response(request_id: str = "req-1") -> dict:
    return {
        "status": "ok",
        "request_id": request_id,
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


class TestDistillClient:
    @respx.mock
    def test_process_maps_reddit_item_to_upstream_contract(self, make_item):
        route = respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(200, json=_response())
        )
        item = make_item(fullname="t3_abc123", title="GME thread", body="raw body")

        call = DistillClient(base_url=BASE_URL, retries=1).process(item)

        sent = json.loads(route.calls[0].request.content)
        assert sent["source"] == "quant_reddit"
        assert sent["source_type"] == "reddit"
        assert sent["source_item_id"] == "t3_abc123"
        assert sent["title"] == "GME thread"
        assert sent["text"] == "raw body"
        assert sent["metadata"]["subreddit"] == "wallstreetbets"
        assert call.request == sent
        assert call.response["sentiment"]["observations"][0]["subject"] == "GME"
        assert call.response["entities"]["items"][0]["ticker"] == "GME"

    @respx.mock
    def test_retries_503_then_succeeds(self, make_item):
        route = respx.post(PROCESS_URL).mock(
            side_effect=[
                httpx.Response(503, json={"status": "error", "detail": "unavailable"}),
                httpx.Response(200, json=_response()),
            ]
        )

        result = DistillClient(
            base_url=BASE_URL, retries=2, backoff=0
        ).process(make_item())

        assert result.response["request_id"] == "req-1"
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
            DistillClient(base_url=BASE_URL, retries=3, backoff=0).process(make_item())

        assert route.call_count == 1

    @respx.mock
    def test_rejects_incomplete_success_response(self, make_item):
        respx.post(PROCESS_URL).mock(
            return_value=httpx.Response(200, json={"status": "ok", "request_id": "req"})
        )

        with pytest.raises(DistillApiError, match="missing distillation"):
            DistillClient(base_url=BASE_URL, retries=1).process(make_item())
