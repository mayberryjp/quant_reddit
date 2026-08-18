"""Ingestion worker entry point (supervisord ``[program:ingest_worker]``).

Fetches Reddit posts/comments, persists them into the ledger, and immediately
submits new items to the shared distillation API.
"""

from __future__ import annotations

import logging
import sys

from app.config import log_config_problems, settings
from app.db import get_engine
from app.repository.postgres import RedditRepository
from app.services import orchestrator
from app.services.distill_client import DistillClient
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
        distill_client=DistillClient(),
        poll_interval=settings.ingest_interval,
    )


if __name__ == "__main__":
    main()
