"""Worker process entry point (supervisord ``[program:worker]``).

Wires the configured Reddit source (OAuth/PRAW or public JSON scrape), Ollama,
and downstream-emitter components into the orchestrator's
``ingest → distill → emit`` loop.
"""

from __future__ import annotations

import logging
import sys

from app.config import log_config_problems
from app.db import get_engine
from app.repository.postgres import RedditRepository
from app.services import orchestrator
from app.services.ollama_client import OllamaClient
from app.services.reddit_client import build_reddit_source
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
    log_config_problems()
    repo = RedditRepository(get_engine())
    reddit_source = build_reddit_source()
    orchestrator.run_forever(
        repo,
        reddit_source=reddit_source,
        llm_client=OllamaClient(),
        sentiment_emitter=SentimentEmitter(repo),
        signal_emitter=SignalEmitter(repo),
    )


if __name__ == "__main__":
    main()
