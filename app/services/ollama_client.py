"""HTTP client for an OpenAI-compatible local LLM endpoint.

Talks to ``POST {OLLAMA_BASE_URL}/chat/completions`` with
``response_format={"type": "json_object"}`` so the model is constrained to emit
valid JSON. The client returns the assistant message content (a JSON *string*);
parsing/validation is the distiller's job.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

log = logging.getLogger("quant_reddit.ollama_client")


class OllamaError(RuntimeError):
    """Raised when the Ollama endpoint cannot be reached or returns an error."""


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float = 0.5,
    ) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout if timeout is not None else settings.http_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self.backoff = backoff

    def chat(self, system: str, user: str) -> str:
        """Send a system+user chat turn and return the assistant JSON content.

        Retries transient failures with exponential backoff. Raises
        :class:`OllamaError` after exhausting attempts.
        """
        payload = {
            "model": self.model,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        url = f"{self.base_url}/chat/completions"
        attempts = max(1, self.retries)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return ""
                return ((choices[0].get("message") or {}).get("content", "") or "")
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(self.backoff * (2**attempt))
        raise OllamaError(
            f"Ollama request to {url} failed after {attempts} attempt(s)"
        ) from last_exc
