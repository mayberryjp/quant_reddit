"""Signal emission → ``quant_signals`` ``POST /signals`` (watchlist candidates).

Aggregates per-ticker findings over a window (confidence-weighted), applies the
``MIN_MENTIONS`` / ``WATCHLIST_MIN_SCORE`` thresholds, and emits one watchlist
candidate per qualifying ticker per day.

``quant_signals`` always responds ``201`` and reports the outcome in the JSON body
(``status`` ∈ accepted | duplicate | unresolved); the emitter classifies from the
body, not the HTTP code, and records every attempt in ``emission_log``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.config import settings
from app.models.domain import (
    Direction,
    EmissionRecord,
    EmissionStatus,
    EmissionTarget,
    TickerFinding,
)
from app.repository.postgres import RedditRepository
from app.services.http import post_json, response_json
from app.timeutil import utcnow

log = logging.getLogger("quant_reddit.signal_emitter")

TAGS = ["reddit", "llm"]

_DELIVERED = (EmissionStatus.accepted, EmissionStatus.duplicate, EmissionStatus.unresolved)
_STATUS_MAP = {
    "accepted": EmissionStatus.accepted,
    "duplicate": EmissionStatus.duplicate,
    "unresolved": EmissionStatus.unresolved,
}


@dataclass
class TickerAggregate:
    ticker: str
    mention_count: int
    reddit_fullnames: list[str]
    avg_sentiment: float
    avg_confidence: float
    direction: Direction
    score: float
    top_rationale: str = ""


def _direction_from(sentiment: float, neutral_band: float) -> Direction:
    if sentiment > neutral_band:
        return Direction.long
    if sentiment < -neutral_band:
        return Direction.short
    return Direction.neutral


def conviction_score(
    mention_count: int,
    avg_abs_sentiment: float,
    avg_confidence: float,
    *,
    volume_saturation: float = 10.0,
) -> float:
    """Normalized conviction in [0, 1] as f(mention volume, |sentiment|, confidence)."""
    volume_factor = (
        min(mention_count / volume_saturation, 1.0) if volume_saturation > 0 else 1.0
    )
    raw = 0.4 * volume_factor + 0.3 * (avg_abs_sentiment / 100.0) + 0.3 * avg_confidence
    return max(0.0, min(1.0, raw))


def aggregate_findings(
    pairs: Iterable[tuple[str, TickerFinding]],
    *,
    neutral_band: float,
    volume_saturation: float = 10.0,
) -> dict[str, TickerAggregate]:
    """Aggregate ``(reddit_fullname, finding)`` pairs into per-ticker aggregates."""
    groups: dict[str, list[tuple[str, TickerFinding]]] = defaultdict(list)
    for fullname, finding in pairs:
        groups[finding.ticker].append((fullname, finding))

    result: dict[str, TickerAggregate] = {}
    for ticker, entries in groups.items():
        findings = [f for _, f in entries]
        fullnames = list(dict.fromkeys(fn for fn, _ in entries))
        mention_count = len(findings)
        weights = [max(f.confidence, 0.0) for f in findings]
        if sum(weights) > 0:
            avg_sentiment = sum(
                f.sentiment_score * w for f, w in zip(findings, weights)
            ) / sum(weights)
        else:
            avg_sentiment = sum(f.sentiment_score for f in findings) / mention_count
        avg_confidence = sum(f.confidence for f in findings) / mention_count
        top = max(findings, key=lambda f: f.confidence)
        result[ticker] = TickerAggregate(
            ticker=ticker,
            mention_count=mention_count,
            reddit_fullnames=fullnames,
            avg_sentiment=avg_sentiment,
            avg_confidence=avg_confidence,
            direction=_direction_from(avg_sentiment, neutral_band),
            score=conviction_score(
                mention_count,
                abs(avg_sentiment),
                avg_confidence,
                volume_saturation=volume_saturation,
            ),
            top_rationale=top.rationale,
        )
    return result


def signal_idempotency_key(source: str, day: date, ticker: str) -> str:
    day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return f"{source}:{day_str}:{ticker}"


def build_signal_request(
    agg: TickerAggregate,
    *,
    source: str,
    day: date,
    model: str,
    prompt_version: str,
    window: str,
) -> dict:
    reason = f"{agg.mention_count} reddit mention(s); avg sentiment {agg.avg_sentiment:.0f}"
    if agg.top_rationale:
        reason = f"{reason}. {agg.top_rationale}"
    return {
        "source": source,
        "idempotency_key": signal_idempotency_key(source, day, agg.ticker),
        "ticker": agg.ticker,
        "signal_type": "watchlist_candidate",
        "direction": agg.direction.value,
        "score": round(agg.score, 4),
        "confidence": round(agg.avg_confidence, 4),
        "reason": reason[: settings.max_reason_length],
        "tags": list(TAGS),
        "metadata": {
            "reddit_fullnames": agg.reddit_fullnames,
            "mention_count": agg.mention_count,
            "model": model,
            "prompt_version": prompt_version,
            "window": window,
        },
    }


class SignalEmitter:
    def __init__(
        self,
        repo: RedditRepository,
        *,
        base_url: str | None = None,
        source: str | None = None,
        min_mentions: int | None = None,
        watchlist_min_score: float | None = None,
        neutral_band: float | None = None,
        volume_saturation: float = 10.0,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float = 0.5,
    ) -> None:
        self.repo = repo
        self.base_url = (base_url or settings.quant_signals_url).rstrip("/")
        self.source = source or settings.signal_source
        self.min_mentions = min_mentions if min_mentions is not None else settings.min_mentions
        self.watchlist_min_score = (
            watchlist_min_score
            if watchlist_min_score is not None
            else settings.watchlist_min_score
        )
        self.neutral_band = neutral_band if neutral_band is not None else settings.neutral_band
        self.volume_saturation = volume_saturation
        self.timeout = timeout if timeout is not None else settings.http_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self.backoff = backoff

    def emit_aggregate(
        self,
        agg: TickerAggregate,
        *,
        model: str,
        prompt_version: str,
        window: str,
        day: date | None = None,
    ) -> EmissionRecord | None:
        """Emit one watchlist candidate if it clears the thresholds, else None."""
        if agg.mention_count < self.min_mentions or agg.score < self.watchlist_min_score:
            return None
        day = day or utcnow().date()
        key = signal_idempotency_key(self.source, day, agg.ticker)
        existing = self.repo.get_emission(EmissionTarget.signals, key)
        if existing is not None and existing.status in _DELIVERED:
            return existing

        body = build_signal_request(
            agg,
            source=self.source,
            day=day,
            model=model,
            prompt_version=prompt_version,
            window=window,
        )
        resp = post_json(
            f"{self.base_url}/signals",
            body,
            timeout=self.timeout,
            retries=self.retries,
            backoff=self.backoff,
        )

        if resp is None:
            status, http_status, response_id = EmissionStatus.failed, None, None
        elif resp.status_code in (200, 201):
            data = response_json(resp)
            status = _STATUS_MAP.get(data.get("status"), EmissionStatus.accepted)
            http_status = resp.status_code
            response_id = data.get("signal_cache_id")
        else:
            status, http_status, response_id = EmissionStatus.failed, resp.status_code, None

        return self.repo.record_emission(
            target=EmissionTarget.signals,
            idempotency_key=key,
            status=status,
            ticker=agg.ticker,
            request=body,
            http_status=http_status,
            response_id=response_id,
        )

    def emit_all(
        self,
        pairs: Iterable[tuple[str, TickerFinding]],
        *,
        model: str,
        prompt_version: str,
        window: str,
        day: date | None = None,
    ) -> list[EmissionRecord]:
        aggregates = aggregate_findings(
            pairs,
            neutral_band=self.neutral_band,
            volume_saturation=self.volume_saturation,
        )
        emitted: list[EmissionRecord] = []
        for agg in aggregates.values():
            rec = self.emit_aggregate(
                agg, model=model, prompt_version=prompt_version, window=window, day=day
            )
            if rec is not None:
                emitted.append(rec)
        return emitted
