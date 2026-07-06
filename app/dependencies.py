"""Dependency wiring shared by route handlers.

Handlers call :func:`get_repo`; tests swap the implementation with :func:`set_repo`
so no live database is required.
"""

from __future__ import annotations

from app.db import get_engine
from app.repository.postgres import RedditRepository

_repo: RedditRepository | None = None


def get_repo() -> RedditRepository:
    global _repo
    if _repo is None:
        _repo = RedditRepository(get_engine())
    return _repo


def set_repo(repo: RedditRepository | None) -> None:
    """Override (or clear) the process-wide repository. Used by tests."""
    global _repo
    _repo = repo
