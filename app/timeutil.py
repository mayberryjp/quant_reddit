"""Small time helpers shared across services and repositories.

All timestamps are normalized to the container's local time zone (configured via
the ``TZ`` environment variable, e.g. ``America/New_York``). Database functions
must always operate in the container's local time, never UTC or a fixed offset.
"""

from __future__ import annotations

from datetime import datetime


def to_local(value) -> datetime | None:
    """Coerce a datetime or ISO string to the container's local time zone.

    Aware values are converted to local time. Naive values (e.g. from the SQLite
    test backend, which stores local wall-clock components) are interpreted as
    local time. This round-trips correctly on both PostgreSQL and SQLite.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone()


def localnow() -> datetime:
    """Return the current time as a timezone-aware datetime in the local zone."""
    return datetime.now().astimezone()
