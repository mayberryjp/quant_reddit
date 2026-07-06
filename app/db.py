"""SQLAlchemy engine helpers.

The engine is created lazily from ``DATABASE_URL``. Tests inject their own engine
(typically SQLite) via :func:`set_engine`, so nothing here assumes PostgreSQL is
reachable.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, creating it from DATABASE_URL if needed."""
    global _engine
    if _engine is not None:
        return _engine
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Override (or clear) the process-wide engine. Used by tests."""
    global _engine
    _engine = engine
