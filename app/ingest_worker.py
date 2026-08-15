"""Ingest-only worker entry point (supervisord ``[program:ingest_worker]``).

Fetches Reddit posts/comments and persists them into the ledger. It does not call
the shared distillation API.
"""

from __future__ import annotations

import logging
import sys

from app.config import log_config_problems, settings
from app.db import get_engine
from app.repository.postgres import RedditRepository
from app.services import orchestrator
from app.services.reddit_client import build_reddit_source

SERVICE_NAME = "quant-reddit-ingest-worker"
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
    orchestrator.run_ingest_forever(
        repo,
        reddit_source=reddit_source,
        poll_interval=settings.ingest_interval,
    )


if __name__ == "__main__":
    main()
