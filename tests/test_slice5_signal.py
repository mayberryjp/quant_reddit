"""Slice 5: signal emission → quant_signals (per-finding parity semantics)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import respx

from app.models.domain import EmissionStatus, EmissionTarget, TickerFinding
from app.services.signal_emitter import (
    SignalEmitter,
    signal_idempotency_key,
)

BASE = "http://signals.test:8016"
SOURCE = "reddit-wsb-v1"
MODEL = "llama3.1"
PROMPT = "wsb-distill-v1"
DAY = date(2026, 7, 6)


def _emitter(repo, **overrides) -> SignalEmitter:
    kwargs = dict(
        base_url=BASE,
        source=SOURCE,
        timeout=5,
        retries=1,
        backoff=0,
    )
    kwargs.update(overrides)
    return SignalEmitter(repo, **kwargs)


def _finding(**overrides) -> TickerFinding:
    data = {
        "ticker": "GME",
        "sentiment_score": 80.0,
        "direction": "long",
        "confidence": 0.9,
        "rationale": "short squeeze",
    }
    data.update(overrides)
    return TickerFinding(**data)


class TestEmission:
    @respx.mock
    def test_request_body_mapping(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"signal_cache_id": "sig-1"})
        )
        _emitter(repo).emit_finding(
            "t3_0",
            _finding(),
            model=MODEL,
            prompt_version=PROMPT,
            window="2026-07-06",
            day=DAY,
        )
        sent = json.loads(respx.calls.last.request.content)
        assert sent["source"] == SOURCE
        assert sent["idempotency_key"] == f"{SOURCE}:t3_0:GME:{MODEL}:{PROMPT}"
        assert sent["ticker"] == "GME"
        assert sent["signal_type"] == "cnbc_mention"
        assert sent["direction"] == "long"
        assert 0.0 <= sent["confidence"] <= 1.0
        assert sent["tags"] == ["reddit", "llm"]
        assert sent["metadata"]["reddit_fullname"] == "t3_0"
        assert sent["metadata"]["model"] == MODEL
        assert sent["metadata"]["prompt_version"] == PROMPT
        assert sent["metadata"]["window"] == "2026-07-06"

    @respx.mock
    def test_accepted(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"signal_cache_id": "sig-1"})
        )
        rec = _emitter(repo).emit_finding("t3_0", _finding(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.accepted
        assert rec.response_id == "sig-1"
        assert rec.http_status == 201

    @respx.mock
    def test_duplicate_from_http_200(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(200, json={"signal_cache_id": "sig-1"})
        )
        rec = _emitter(repo).emit_finding("t3_0", _finding(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.duplicate

    @respx.mock
    def test_body_status_is_ignored_when_http_201(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "unresolved", "signal_cache_id": "sig-1"})
        )
        rec = _emitter(repo).emit_finding("t3_0", _finding(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.accepted
        stored = repo.get_emission(EmissionTarget.signals, f"{SOURCE}:t3_0:GME:{MODEL}:{PROMPT}")
        assert stored.status is EmissionStatus.accepted

    @respx.mock
    def test_server_error_failed(self, repo):
        route = respx.post(f"{BASE}/signals").mock(return_value=httpx.Response(500))
        rec = _emitter(repo, retries=2).emit_finding("t3_0", _finding(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.failed
        assert route.call_count == 2

    @respx.mock
    def test_idempotency_key_and_skip_on_rerun(self, repo):
        route = respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"signal_cache_id": "sig-1"})
        )
        em = _emitter(repo)
        first = em.emit_finding("t3_0", _finding(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        second = em.emit_finding("t3_0", _finding(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert first.status is EmissionStatus.accepted
        assert second.status is EmissionStatus.accepted  # returned existing
        assert route.call_count == 1  # no re-POST for the same key

    @respx.mock
    def test_emit_all_submits_each_item_ticker(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"signal_cache_id": "sig-x"})
        )
        pairs = [
            ("t3_a", _finding(ticker="GME")),
            ("t3_b", _finding(ticker="GME")),
            ("t3_c", _finding(ticker="AMC")),
        ]
        emitted = _emitter(repo).emit_all(pairs, model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        tickers = {rec.ticker for rec in emitted}
        assert tickers == {"GME", "AMC"}
        assert len(emitted) == 3


class TestKey:
    def test_idempotency_key_format(self):
        assert signal_idempotency_key(SOURCE, "t3_abc", "GME", MODEL, PROMPT) == f"{SOURCE}:t3_abc:GME:{MODEL}:{PROMPT}"
