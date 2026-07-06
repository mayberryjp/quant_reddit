"""Slice 5: signal emission → quant_signals (aggregation, gating, mapping)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import respx

from app.models.domain import Direction, EmissionStatus, EmissionTarget, TickerFinding
from app.services.signal_emitter import (
    SignalEmitter,
    TickerAggregate,
    aggregate_findings,
    conviction_score,
    signal_idempotency_key,
)

BASE = "http://signals.test:8016"
SOURCE = "reddit-wsb-v1"
MODEL = "llama3.1"
PROMPT = "wsb-distill-v1"
DAY = date(2026, 7, 6)


def _pairs(ticker="GME", n=5, sentiment=80.0, confidence=0.9, rationale="squeeze"):
    return [
        (f"t3_{i}", TickerFinding(ticker=ticker, sentiment_score=sentiment, confidence=confidence, rationale=rationale))
        for i in range(n)
    ]


def _emitter(repo, **overrides) -> SignalEmitter:
    kwargs = dict(
        base_url=BASE,
        source=SOURCE,
        min_mentions=3,
        watchlist_min_score=0.5,
        neutral_band=20.0,
        volume_saturation=10.0,
        timeout=5,
        retries=1,
        backoff=0,
    )
    kwargs.update(overrides)
    return SignalEmitter(repo, **kwargs)


def _agg(**overrides) -> TickerAggregate:
    data = dict(
        ticker="GME",
        mention_count=5,
        reddit_fullnames=["t3_0", "t3_1", "t3_2", "t3_3", "t3_4"],
        avg_sentiment=80.0,
        avg_confidence=0.9,
        direction=Direction.long,
        score=0.71,
        top_rationale="short squeeze",
    )
    data.update(overrides)
    return TickerAggregate(**data)


class TestAggregation:
    def test_conviction_score_bounds(self):
        assert conviction_score(0, 0, 0) == 0.0
        assert 0.0 <= conviction_score(3, 50, 0.5) <= 1.0
        assert conviction_score(1000, 100, 1.0) == 1.0

    def test_aggregate_math(self):
        aggs = aggregate_findings(_pairs(n=3, sentiment=60, confidence=0.8), neutral_band=20.0)
        agg = aggs["GME"]
        assert agg.mention_count == 3
        assert agg.reddit_fullnames == ["t3_0", "t3_1", "t3_2"]
        assert agg.direction is Direction.long
        assert abs(agg.avg_sentiment - 60.0) < 1e-6
        assert 0.0 <= agg.score <= 1.0

    def test_confidence_weighting(self):
        pairs = [
            ("t3_a", TickerFinding(ticker="GME", sentiment_score=100, confidence=1.0)),
            ("t3_b", TickerFinding(ticker="GME", sentiment_score=0, confidence=0.0)),
        ]
        agg = aggregate_findings(pairs, neutral_band=20.0)["GME"]
        # weight for the zero-confidence finding is 0 -> weighted avg pulled to 100
        assert agg.avg_sentiment == 100.0

    def test_direction_short(self):
        agg = aggregate_findings(_pairs(sentiment=-70.0), neutral_band=20.0)["GME"]
        assert agg.direction is Direction.short

    def test_direction_neutral_within_band(self):
        agg = aggregate_findings(_pairs(sentiment=10.0), neutral_band=20.0)["GME"]
        assert agg.direction is Direction.neutral


class TestGating:
    @respx.mock
    def test_below_min_mentions_not_emitted(self, repo):
        route = respx.post(f"{BASE}/signals").mock(return_value=httpx.Response(201))
        rec = _emitter(repo).emit_aggregate(
            _agg(mention_count=2), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY
        )
        assert rec is None
        assert route.called is False

    @respx.mock
    def test_below_min_score_not_emitted(self, repo):
        route = respx.post(f"{BASE}/signals").mock(return_value=httpx.Response(201))
        rec = _emitter(repo).emit_aggregate(
            _agg(score=0.3), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY
        )
        assert rec is None
        assert route.called is False


class TestEmission:
    @respx.mock
    def test_request_body_mapping(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "signal_cache_id": "sig-1"})
        )
        _emitter(repo).emit_aggregate(
            _agg(), model=MODEL, prompt_version=PROMPT, window="2026-07-06", day=DAY
        )
        sent = json.loads(respx.calls.last.request.content)
        assert sent["source"] == SOURCE
        assert sent["idempotency_key"] == f"{SOURCE}:2026-07-06:GME"
        assert sent["ticker"] == "GME"
        assert sent["signal_type"] == "watchlist_candidate"
        assert sent["direction"] == "long"
        assert 0.0 <= sent["score"] <= 1.0
        assert 0.0 <= sent["confidence"] <= 1.0
        assert sent["tags"] == ["wallstreetbets", "reddit", "llm"]
        assert sent["metadata"]["reddit_fullnames"] == _agg().reddit_fullnames
        assert sent["metadata"]["mention_count"] == 5
        assert sent["metadata"]["model"] == MODEL
        assert sent["metadata"]["prompt_version"] == PROMPT
        assert sent["metadata"]["window"] == "2026-07-06"

    @respx.mock
    def test_accepted(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "signal_cache_id": "sig-1"})
        )
        rec = _emitter(repo).emit_aggregate(_agg(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.accepted
        assert rec.response_id == "sig-1"
        assert rec.http_status == 201

    @respx.mock
    def test_duplicate_from_body(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "duplicate", "signal_cache_id": "sig-1"})
        )
        rec = _emitter(repo).emit_aggregate(_agg(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.duplicate

    @respx.mock
    def test_unresolved_from_body(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "unresolved", "signal_cache_id": "sig-1"})
        )
        rec = _emitter(repo).emit_aggregate(_agg(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.unresolved
        stored = repo.get_emission(EmissionTarget.signals, f"{SOURCE}:2026-07-06:GME")
        assert stored.status is EmissionStatus.unresolved

    @respx.mock
    def test_server_error_failed(self, repo):
        route = respx.post(f"{BASE}/signals").mock(return_value=httpx.Response(500))
        rec = _emitter(repo, retries=2).emit_aggregate(_agg(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert rec.status is EmissionStatus.failed
        assert route.call_count == 2

    @respx.mock
    def test_daily_idempotency_key_and_skip_on_rerun(self, repo):
        route = respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "signal_cache_id": "sig-1"})
        )
        em = _emitter(repo)
        first = em.emit_aggregate(_agg(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        second = em.emit_aggregate(_agg(), model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        assert first.status is EmissionStatus.accepted
        assert second.status is EmissionStatus.accepted  # returned existing
        assert route.call_count == 1  # no re-POST for the same daily key

    @respx.mock
    def test_emit_all_filters_low_volume(self, repo):
        respx.post(f"{BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "signal_cache_id": "sig-x"})
        )
        pairs = _pairs("GME", n=5) + _pairs("AMC", n=2)  # AMC below min_mentions=3
        emitted = _emitter(repo).emit_all(pairs, model=MODEL, prompt_version=PROMPT, window="1d", day=DAY)
        tickers = {rec.ticker for rec in emitted}
        assert tickers == {"GME"}


class TestKey:
    def test_idempotency_key_format(self):
        assert signal_idempotency_key(SOURCE, DAY, "GME") == f"{SOURCE}:2026-07-06:GME"
