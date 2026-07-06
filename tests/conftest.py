"""Shared pytest fixtures.

Tests run against an in-memory SQLite database (no Docker/PostgreSQL required).
The ``reddit`` schema is translated to SQLite's default schema, and the
repository's SQLAlchemy Core statements run unchanged against both backends.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from webtest import TestApp

from app import db, dependencies
from app.models.domain import ProcessState, RedditItem, RedditKind
from app.repository.postgres import RedditRepository
from app.repository.schema import metadata


def make_reddit_item(**overrides) -> RedditItem:
    """Build a valid :class:`RedditItem` for tests."""
    now = datetime.now(timezone.utc)
    data = dict(
        fullname="t3_abc123",
        kind=RedditKind.post,
        subreddit="wallstreetbets",
        author="wsb_user",
        title="YOLO into GME",
        body="diamond hands, GME to the moon",
        score=4200,
        permalink="/r/wallstreetbets/comments/abc123/",
        parent_fullname=None,
        created_utc=now,
        fetched_at=now,
        process_state=ProcessState.new,
    )
    data.update(overrides)
    return RedditItem(**data)


@pytest.fixture
def make_item():
    """Expose the item factory as a fixture."""
    return make_reddit_item


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    ).execution_options(schema_translate_map={"reddit": None})
    metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def repo(engine):
    return RedditRepository(engine)


@pytest.fixture
def app_client(engine):
    """A WebTest client wired to the in-memory database."""
    db.set_engine(engine)
    dependencies.set_repo(RedditRepository(engine))
    from app.main import create_app

    client = TestApp(create_app())
    try:
        yield client
    finally:
        dependencies.set_repo(None)
        db.set_engine(None)
