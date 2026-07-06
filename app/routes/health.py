"""Health and operational-visibility routes.

Slice 0 provides the liveness probe ``/reddit/health`` (no database dependency).
Readiness and stats are added in Slice 7.
"""

from __future__ import annotations

from bottle import Bottle

sub = Bottle()


@sub.get("/reddit/health")
def health():
    """Liveness probe. Does not depend on the database."""
    return {"status": "ok"}
