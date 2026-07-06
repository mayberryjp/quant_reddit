"""Health, readiness, and operational-visibility routes.

``/reddit/health`` is a liveness probe (no DB). ``/reddit/ready`` returns 503 when
the database is unreachable. ``/reddit/stats`` reports operational counters and the
worker heartbeat.
"""

from __future__ import annotations

from bottle import Bottle, response

from app.dependencies import get_repo
from app.timeutil import to_utc

sub = Bottle()


@sub.get("/reddit/health")
def health():
    """Liveness probe. Does not depend on the database."""
    return {"status": "ok"}


@sub.get("/reddit/ready")
def ready():
    """Readiness probe. Fails (503) when the database is unreachable."""
    database_ok = get_repo().ping()
    if not database_ok:
        response.status = 503
    return {
        "status": "ready" if database_ok else "not_ready",
        "database": "ok" if database_ok else "unavailable",
    }


@sub.get("/reddit/stats")
def stats():
    """Operational counters computed from the ledger, plus worker heartbeat."""
    repo = get_repo()
    data = repo.stats()
    last_fetched = to_utc(data["last_fetched_at"])
    heartbeat = to_utc(repo.get_heartbeat())
    return {
        "items_ingested": data["items_ingested"],
        "items_by_state": data["items_by_state"],
        "extractions": data["extractions"],
        "emissions": data["emissions"],
        "last_fetched_at": last_fetched.isoformat() if last_fetched else None,
        "last_run": heartbeat.isoformat() if heartbeat else None,
    }
