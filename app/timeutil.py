"""Small time helpers shared across services and repositories."""

from __future__ import annotations

from datetime import datetime, timezone


def to_utc(value) -> datetime | None:
    """Coerce a datetime or ISO string (possibly naive, e.g. from SQLite) to UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)
