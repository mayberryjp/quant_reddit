"""Client for the shared quant_distill async job API.

``POST /v1/process`` enqueues a job and returns ``202`` + ``job_id`` immediately;
the pipeline result (2-30 minutes, chunked map/reduce against Ollama) is fetched
later via ``GET /v1/jobs/{job_id}``. See quant_distill's consumer guide.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.models.domain import RedditItem

log = logging.getLogger("quant_reddit.distill_client")

# Submission errors that must not be retried unchanged (bad request/payload).
_NON_RETRYABLE_STATUSES = {422, 413}


class DistillApiError(RuntimeError):
    """Raised when quant_distill cannot return a valid successful response."""


@dataclass(frozen=True)
class DistillCall:
    request: dict
    response: dict


def build_process_request(item: RedditItem) -> dict:
    text = item.body.strip() or (item.title or "").strip()
    metadata = {
        "kind": item.kind.value,
        "subreddit": item.subreddit,
        "author": item.author,
        "permalink": item.permalink,
        "parent_fullname": item.parent_fullname,
        "score": item.score,
    }
    return {
        "source": "quant_reddit",
        "source_type": "reddit",
        "source_item_id": item.fullname,
        "title": item.title,
        "text": text,
        "observed_at": item.created_utc.isoformat(),
        "metadata": {key: value for key, value in metadata.items() if value is not None},
    }


class DistillClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float = 0.5,
    ) -> None:
        base = (base_url or settings.quant_distill_url).rstrip("/")
        self.process_url = f"{base}/v1/process"
        self.jobs_url = f"{base}/v1/jobs"
        self.timeout = timeout if timeout is not None else settings.distill_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self.backoff = backoff

    def submit(self, item: RedditItem) -> tuple[str, dict]:
        """Enqueue a job for ``item``. Returns ``(job_id, request)``."""
        request = build_process_request(item)
        if not request["text"]:
            raise DistillApiError("source item has no text to distill")

        response = self._request("POST", self.process_url, json=request)
        payload = self._parse_json(response)
        if response.status_code != 202:
            detail = payload.get("detail") or payload.get("error") or response.text
            raise DistillApiError(
                f"quant_distill returned HTTP {response.status_code}: {detail}"
            )
        job_id = payload.get("job_id")
        if not job_id:
            raise DistillApiError("quant_distill accepted response is missing job_id")
        return job_id, request

    def get_job(self, job_id: str) -> dict:
        """Fetch current status for a previously submitted job."""
        response = self._request("GET", f"{self.jobs_url}/{job_id}")
        payload = self._parse_json(response)
        if response.status_code == 404:
            raise DistillApiError(f"quant_distill job not found: {job_id}")
        if response.status_code != 200:
            detail = payload.get("detail") or payload.get("error") or response.text
            raise DistillApiError(
                f"quant_distill returned HTTP {response.status_code}: {detail}"
            )
        if payload.get("status") not in {"queued", "running", "succeeded", "failed"}:
            raise DistillApiError("quant_distill job response is missing status")
        return payload

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DistillApiError("quant_distill returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DistillApiError("quant_distill returned a non-object response")
        return payload

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempts = max(1, self.retries)
        last_error: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                log.warning(
                    "quant_distill request failed (attempt %d/%d)",
                    attempt + 1,
                    attempts,
                )
                time.sleep(self.backoff * (2**attempt))
                continue
            if response.status_code in _NON_RETRYABLE_STATUSES:
                return response
            if response.status_code >= 500 and attempt + 1 < attempts:
                time.sleep(self.backoff * (2**attempt))
                continue
            return response
        raise DistillApiError("quant_distill request failed") from last_error
