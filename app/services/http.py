"""Shared HTTP helper for downstream emitters.

Provides a small ``post_json`` with timeout and retry/backoff on transport errors
and 5xx responses, so the sentiment and signal emitters share one battle-tested
path. 4xx responses are returned as-is (not retried) for the caller to classify.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("quant_reddit.http")


def post_json(
    url: str,
    body: dict,
    *,
    timeout: float,
    retries: int,
    backoff: float = 0.5,
) -> httpx.Response | None:
    """POST ``body`` as JSON. Retries transport errors and 5xx up to ``retries``
    attempts. Returns the final :class:`httpx.Response`, or ``None`` if every
    attempt raised a transport error.
    """
    attempts = max(1, retries)
    for attempt in range(attempts):
        last = attempt + 1 >= attempts
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=body)
        except httpx.HTTPError:
            log.warning("POST %s failed (attempt %d/%d)", url, attempt + 1, attempts)
            if last:
                return None
            time.sleep(backoff * (2**attempt))
            continue
        if 500 <= resp.status_code < 600 and not last:
            time.sleep(backoff * (2**attempt))
            continue
        return resp
    return None


def response_json(resp: httpx.Response) -> dict:
    """Best-effort JSON body as a dict (empty dict on any parse failure)."""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
