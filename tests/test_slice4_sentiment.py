"""Slice 4: sentiment emission → quant_sentiment (mocked HTTP)."""

from __future__ import annotations

import json

import httpx
import respx

from app.models.domain import EmissionStatus, EmissionTarget, TickerFinding
from app.services.sentiment_emitter import SentimentEmitter, sentiment_idempotency_key

BASE = "http://sentiment.test:8017"
SOURCE = "reddit-wsb-v1"
MODEL = "llama3.1"
PROMPT = "wsb-distill-v1"


def _finding(**overrides) -> TickerFinding:
    data = dict(
        ticker="GME",
        sentiment_score=80.0,
        confidence=0.9,
        is_watchlist_candidate=True,
        rationale="short squeeze",
    )
    data.update(overrides)
    return TickerFinding(**data)


def _emitter(repo, **overrides) -> SentimentEmitter:
    kwargs = dict(
        base_url=BASE,
        source=SOURCE,
        source_weight=0.5,
        timeout=5,
        retries=1,
        backoff=0,
    )
    kwargs.update(overrides)
    return SentimentEmitter(repo, **kwargs)


class TestRequestMapping:
    @respx.mock
    def test_request_body_exact(self, repo, make_item):
        item = make_item(fullname="t3_abc", permalink="https://reddit.com/x")
        route = respx.post(f"{BASE}/sentiment").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "sentiment_id": "obs-1"})
        )
        _emitter(repo).emit(item, _finding(), model=MODEL, prompt_version=PROMPT)
        body = respx.calls.last.request

        sent = json.loads(body.content)
        assert sent["source"] == SOURCE
        assert sent["idempotency_key"] == f"{SOURCE}:t3_abc:GME"
        assert sent["subject_type"] == "ticker"
        assert sent["subject"] == "GME"
        assert sent["sentiment_score"] == 80.0
        assert sent["confidence"] == 0.9
        assert sent["source_weight"] == 0.5
        assert sent["reason"] == "short squeeze"
        assert sent["observed_at"] == item.created_utc.isoformat()
        assert sent["tags"] == ["reddit", "wallstreetbets"]
        assert sent["metadata"] == {
            "reddit_fullname": "t3_abc",
            "permalink": "https://reddit.com/x",
            "model": MODEL,
            "prompt_version": PROMPT,
        }
        # sentiment_label must never be sent (derived server-side)
        assert "sentiment_label" not in sent
        assert route.called

    def test_idempotency_key_format(self):
        assert sentiment_idempotency_key(SOURCE, "t3_abc", "GME") == f"{SOURCE}:t3_abc:GME"


class TestOutcomes:
    @respx.mock
    def test_accepted_201(self, repo, make_item):
        item = make_item(fullname="t3_a")
        respx.post(f"{BASE}/sentiment").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "sentiment_id": "obs-1"})
        )
        rec = _emitter(repo).emit(item, _finding(), model=MODEL, prompt_version=PROMPT)
        assert rec.status is EmissionStatus.accepted
        assert rec.http_status == 201
        assert rec.response_id == "obs-1"
        stored = repo.get_emission(EmissionTarget.sentiment, f"{SOURCE}:t3_a:GME")
        assert stored.status is EmissionStatus.accepted

    @respx.mock
    def test_duplicate_200(self, repo, make_item):
        item = make_item(fullname="t3_b")
        respx.post(f"{BASE}/sentiment").mock(
            return_value=httpx.Response(200, json={"status": "duplicate", "sentiment_id": "obs-2"})
        )
        rec = _emitter(repo).emit(item, _finding(), model=MODEL, prompt_version=PROMPT)
        assert rec.status is EmissionStatus.duplicate
        assert rec.http_status == 200

    @respx.mock
    def test_validation_error_422_failed(self, repo, make_item):
        item = make_item(fullname="t3_c")
        respx.post(f"{BASE}/sentiment").mock(
            return_value=httpx.Response(422, json={"detail": "bad"})
        )
        rec = _emitter(repo).emit(item, _finding(), model=MODEL, prompt_version=PROMPT)
        assert rec.status is EmissionStatus.failed
        assert rec.http_status == 422

    @respx.mock
    def test_server_error_retries_then_failed(self, repo, make_item):
        item = make_item(fullname="t3_d")
        route = respx.post(f"{BASE}/sentiment").mock(return_value=httpx.Response(500))
        rec = _emitter(repo, retries=3).emit(item, _finding(), model=MODEL, prompt_version=PROMPT)
        assert rec.status is EmissionStatus.failed
        assert rec.http_status == 500
        assert route.call_count == 3  # retried

    @respx.mock
    def test_network_error_failed(self, repo, make_item):
        item = make_item(fullname="t3_e")
        route = respx.post(f"{BASE}/sentiment").mock(side_effect=httpx.ConnectError("down"))
        rec = _emitter(repo, retries=2).emit(item, _finding(), model=MODEL, prompt_version=PROMPT)
        assert rec.status is EmissionStatus.failed
        assert rec.http_status is None
        assert route.call_count == 2

    @respx.mock
    def test_skip_if_already_delivered(self, repo, make_item):
        item = make_item(fullname="t3_f")
        key = f"{SOURCE}:t3_f:GME"
        repo.record_emission(
            target=EmissionTarget.sentiment,
            idempotency_key=key,
            status=EmissionStatus.accepted,
            ticker="GME",
        )
        route = respx.post(f"{BASE}/sentiment").mock(return_value=httpx.Response(201))
        rec = _emitter(repo).emit(item, _finding(), model=MODEL, prompt_version=PROMPT)
        assert rec.status is EmissionStatus.accepted
        assert route.called is False  # no re-POST on re-run
