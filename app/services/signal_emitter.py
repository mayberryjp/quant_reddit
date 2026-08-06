"""Signal emission → ``quant_signals`` ``POST /signals``.

Parity behavior with the CNBC producer:
* One POST per resolved finding (no aggregate threshold gating).
* Version-scoped idempotency key includes source, item, ticker, model, prompt.
* Outcome classification is based on HTTP status code (201 accepted, 200 duplicate).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable

from app.config import settings
from app.models.domain import Direction, EmissionRecord, EmissionStatus, EmissionTarget, TickerFinding
from app.repository.postgres import RedditRepository
from app.services.http import post_json, response_json

log = logging.getLogger("quant_reddit.signal_emitter")

TAGS = ["reddit", "llm"]

_DELIVERED = (EmissionStatus.accepted, EmissionStatus.duplicate)


def signal_idempotency_key(
    source: str,
    reddit_fullname: str,
    ticker: str,
    model: str,
    prompt_version: str,
) -> str:
    return f"{source}:{reddit_fullname}:{ticker}:{model}:{prompt_version}"


def build_signal_request(
    reddit_fullname: str,
    finding: TickerFinding,
    *,
    source: str,
    model: str,
    prompt_version: str,
    window: str,
) -> dict:
    direction = finding.direction
    if not isinstance(direction, Direction):
        direction = Direction.neutral
    return {
        "source": source,
        "idempotency_key": signal_idempotency_key(
            source, reddit_fullname, finding.ticker, model, prompt_version
        ),
        "ticker": finding.ticker,
        "signal_type": settings.watchlist_signal_type,
        "direction": direction.value,
        "confidence": finding.confidence,
        "reason": ((finding.context or finding.rationale) or "")[: settings.max_reason_length],
        "tags": list(TAGS),
        "metadata": {
            "reddit_fullname": reddit_fullname,
            "model": model,
            "prompt_version": prompt_version,
            "window": window,
            "guest": finding.speaker,
            "company_name": finding.company_name,
            "raw_mention": finding.raw_mention,
        },
    }


class SignalEmitter:
    def __init__(
        self,
        repo: RedditRepository,
        *,
        base_url: str | None = None,
        source: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float = 0.5,
    ) -> None:
        self.repo = repo
        self.base_url = (base_url or settings.quant_signals_url).rstrip("/")
        self.source = source or settings.signal_source
        self.timeout = timeout if timeout is not None else settings.http_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self.backoff = backoff

    def emit_finding(
        self,
        reddit_fullname: str,
        finding: TickerFinding,
        *,
        model: str,
        prompt_version: str,
        window: str,
        day: date | None = None,
    ) -> EmissionRecord:
        """Emit one watchlist signal for a resolved ticker finding."""
        _ = day  # kept for API compatibility with existing callers
        key = signal_idempotency_key(
            self.source, reddit_fullname, finding.ticker, model, prompt_version
        )
        existing = self.repo.get_emission(EmissionTarget.signals, key)
        if existing is not None and existing.status in _DELIVERED:
            return existing

        body = build_signal_request(
            reddit_fullname,
            finding,
            source=self.source,
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

        response_id = None
        if resp is None:
            status, http_status, response_id = EmissionStatus.failed, None, None
        elif resp.status_code == 201:
            status, http_status = EmissionStatus.accepted, 201
            data = response_json(resp)
            response_id = data.get("signal_cache_id")
        elif resp.status_code == 200:
            status, http_status = EmissionStatus.duplicate, 200
            data = response_json(resp)
            response_id = data.get("signal_cache_id")
        else:
            status, http_status, response_id = EmissionStatus.failed, resp.status_code, None

        return self.repo.record_emission(
            target=EmissionTarget.signals,
            idempotency_key=key,
            status=status,
            ticker=finding.ticker,
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
        emitted: list[EmissionRecord] = []
        for reddit_fullname, finding in pairs:
            rec = self.emit_finding(
                reddit_fullname,
                finding,
                model=model,
                prompt_version=prompt_version,
                window=window,
                day=day,
            )
            emitted.append(rec)
        return emitted
