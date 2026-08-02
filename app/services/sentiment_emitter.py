"""Sentiment emission → ``quant_sentiment`` ``POST /sentiment``.

Maps each validated ticker finding to one sentiment observation (one observation
per (item, ticker), per the spec and owner guidance) and records every attempt in
``emission_log``. ``sentiment_label`` is intentionally never sent — it is derived
server-side by ``quant_sentiment``.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.models.domain import (
    EmissionRecord,
    EmissionStatus,
    EmissionTarget,
    RedditItem,
    TickerFinding,
)
from app.repository.postgres import RedditRepository
from app.services.http import post_json, response_json

log = logging.getLogger("quant_reddit.sentiment_emitter")

# Emission outcomes that mean the observation was already delivered; re-emitting
# is skipped so re-runs are idempotent no-ops.
_DELIVERED = (EmissionStatus.accepted, EmissionStatus.duplicate)


def sentiment_idempotency_key(source: str, reddit_fullname: str, ticker: str) -> str:
    return f"{source}:{reddit_fullname}:{ticker}"


def build_sentiment_request(
    item: RedditItem,
    finding: TickerFinding,
    *,
    source: str,
    source_weight: float,
    model: str,
    prompt_version: str,
) -> dict:
    return {
        "source": source,
        "idempotency_key": sentiment_idempotency_key(source, item.fullname, finding.ticker),
        "subject_type": "ticker",
        "subject": finding.ticker,
        "sentiment_score": finding.sentiment_score,
        "confidence": finding.confidence,
        "source_weight": source_weight,
        "reason": (finding.rationale or "")[: settings.max_reason_length],
        "observed_at": item.created_utc.isoformat(),
        "tags": ["reddit", item.subreddit],
        "metadata": {
            "reddit_fullname": item.fullname,
            "permalink": item.permalink,
            "model": model,
            "prompt_version": prompt_version,
        },
    }


class SentimentEmitter:
    def __init__(
        self,
        repo: RedditRepository,
        *,
        base_url: str | None = None,
        source: str | None = None,
        source_weight: float | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float = 0.5,
    ) -> None:
        self.repo = repo
        self.base_url = (base_url or settings.quant_sentiment_url).rstrip("/")
        self.source = source or settings.sentiment_source
        self.source_weight = (
            source_weight if source_weight is not None else settings.source_weight
        )
        self.timeout = timeout if timeout is not None else settings.http_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self.backoff = backoff

    def emit(
        self,
        item: RedditItem,
        finding: TickerFinding,
        *,
        model: str,
        prompt_version: str,
    ) -> EmissionRecord:
        key = sentiment_idempotency_key(self.source, item.fullname, finding.ticker)
        existing = self.repo.get_emission(EmissionTarget.sentiment, key)
        if existing is not None and existing.status in _DELIVERED:
            return existing

        body = build_sentiment_request(
            item,
            finding,
            source=self.source,
            source_weight=self.source_weight,
            model=model,
            prompt_version=prompt_version,
        )
        resp = post_json(
            f"{self.base_url}/sentiment",
            body,
            timeout=self.timeout,
            retries=self.retries,
            backoff=self.backoff,
        )

        if resp is None:
            status, http_status, response_id = EmissionStatus.failed, None, None
        elif resp.status_code == 201:
            status = EmissionStatus.accepted
            http_status = 201
            response_id = response_json(resp).get("sentiment_id")
        elif resp.status_code == 200:
            status = EmissionStatus.duplicate
            http_status = 200
            response_id = response_json(resp).get("sentiment_id")
        else:
            status = EmissionStatus.failed
            http_status = resp.status_code
            response_id = None

        return self.repo.record_emission(
            target=EmissionTarget.sentiment,
            idempotency_key=key,
            status=status,
            ticker=finding.ticker,
            request=body,
            http_status=http_status,
            response_id=response_id,
        )
