"""Shared pytest fixtures.

Tests run against an in-memory SQLite database (no Docker/PostgreSQL required).
The ``reddit`` schema is translated to SQLite's default schema, and the
repository's SQLAlchemy Core statements run unchanged against both backends.
"""

from __future__ import annotations

import pytest
from webtest import TestApp


@pytest.fixture
def app_client():
    """A WebTest client wired to the Bottle application.

    Slice 0 exposes only the liveness probe, which needs no database. The
    fixture is extended with database wiring in Slice 1.
    """
    from app.main import create_app

    return TestApp(create_app())
