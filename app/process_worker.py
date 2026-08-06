"""Process-only worker entry point (supervisord ``[program:process_worker]``).

Consumes ``new`` ledger items, runs LLM distillation, and emits sentiment/signals.
It does not fetch Reddit posts/comments.
"""

from __future__ import annotations

import logging
import sys

from app.config import log_config_problems, settings
from app.db import get_engine
from app.repository.postgres import RedditRepository
from app.services import orchestrator
from app.services.ollama_client import OllamaClient
from app.services.sentiment_emitter import SentimentEmitter
from app.services.signal_emitter import SignalEmitter

SERVICE_NAME = "quant-reddit-process-worker"
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
    orchestrator.run_process_forever(
        repo,
        llm_client=OllamaClient(),
        sentiment_emitter=SentimentEmitter(repo),
        signal_emitter=SignalEmitter(repo),
        poll_interval=settings.process_interval,
    )


if __name__ == "__main__":
    main()
