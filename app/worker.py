"""Worker process entry point (supervisord ``[program:worker]``).

Wires the real Reddit (PRAW), Ollama, and downstream-emitter components into the
orchestrator's ``ingest → distill → emit`` loop.
"""

from __future__ import annotations

import logging
import sys

from app.db import get_engine
from app.repository.postgres import RedditRepository
from app.services import orchestrator
from app.services.ollama_client import OllamaClient
from app.services.reddit_client import PrawRedditSource
from app.services.sentiment_emitter import SentimentEmitter
from app.services.signal_emitter import SignalEmitter

SERVICE_NAME = "quant-reddit-worker"
log = logging.getLogger(SERVICE_NAME)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
    force=True,
)


def main() -> None:
    repo = RedditRepository(get_engine())
    orchestrator.run_forever(
        repo,
        reddit_source=PrawRedditSource.from_settings(),
        llm_client=OllamaClient(),
        sentiment_emitter=SentimentEmitter(repo),
        signal_emitter=SignalEmitter(repo),
    )


if __name__ == "__main__":
    main()
