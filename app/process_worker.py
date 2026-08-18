"""Process-only worker entry point (supervisord ``[program:process_worker]``).

Polls ``submitted`` quant_distill jobs for completion each cycle. It does not
fetch Reddit posts/comments or submit new work.
"""

from __future__ import annotations

import logging
import sys

from app.config import log_config_problems, settings
from app.db import get_engine
from app.repository.postgres import RedditRepository
from app.services import orchestrator
from app.services.distill_client import DistillClient

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
        distill_client=DistillClient(),
        poll_interval=settings.process_interval,
    )


if __name__ == "__main__":
    main()
