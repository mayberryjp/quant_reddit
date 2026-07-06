"""Worker process entry point (supervisord ``[program:worker]``).

Slice 0 provides a minimal heartbeat loop so the container's worker process is
valid. Slice 6 replaces the body with the ``ingest → distill → emit``
orchestration cycle.
"""

from __future__ import annotations

import logging
import sys
import time

from app.config import settings

SERVICE_NAME = "quant-reddit-worker"
log = logging.getLogger(SERVICE_NAME)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
    force=True,
)


def main() -> None:
    log.info("quant_reddit worker starting (poll interval %ss)", settings.poll_interval)
    while True:
        log.info("worker heartbeat")
        time.sleep(settings.poll_interval)


if __name__ == "__main__":
    main()
