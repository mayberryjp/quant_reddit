"""Alembic environment for quant_reddit.

Migrations run against PostgreSQL only (the production system of record). The
database URL is read from the ``DATABASE_URL`` environment variable. Every
object this service owns — the ledger tables *and* Alembic's own version table
(``alembic_version_reddit``) — lives in the dedicated ``reddit`` schema, which
keeps migration state isolated from sibling services that share the database. On
startup any project-owned table still sitting in a pre-``reddit`` location is
relocated into the schema (data-preserving) before migrations run.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

# Every object this service owns is namespaced under the ``reddit`` schema,
# including Alembic's own bookkeeping (version) table.
SCHEMA = "reddit"
VERSION_TABLE = "alembic_version_reddit"

# Project-owned tables that must live in the ``reddit`` schema. Any found in a
# pre-``reddit`` location (e.g. ``public`` on an older deployment) are moved into
# the schema on startup, preserving their data.
PROJECT_TABLES = (
    "reddit_items",
    "llm_extractions",
    "emission_log",
    "ingest_cursor",
    "cycle_runs",
    "distillations",
    VERSION_TABLE,
)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return url


def _consolidate_into_schema(connection) -> None:
    """Move any project-owned table still outside ``reddit`` into the schema.

    ``ALTER TABLE ... SET SCHEMA`` is a metadata-only move, so rows, indexes and
    foreign keys are preserved. A table is moved only when it is absent from
    ``reddit`` but present in the connection's search path, making this a no-op on
    an already-consolidated database and ensuring it never touches a sibling
    service's identically named table that already coexists.
    """
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    for table in PROJECT_TABLES:
        already_here = connection.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = :name"
            ),
            {"schema": SCHEMA, "name": table},
        ).first()
        if already_here is not None:
            continue
        source_schema = connection.execute(
            text(
                "SELECT t.table_schema "
                "FROM information_schema.tables t "
                "JOIN unnest(current_schemas(false)) WITH ORDINALITY AS sp(nspname, ord) "
                "  ON sp.nspname = t.table_schema "
                "WHERE t.table_name = :name AND t.table_schema <> :schema "
                "ORDER BY sp.ord "
                "LIMIT 1"
            ),
            {"schema": SCHEMA, "name": table},
        ).scalar()
        if source_schema is not None:
            connection.execute(
                text(f'ALTER TABLE "{source_schema}"."{table}" SET SCHEMA {SCHEMA}')
            )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), pool_pre_ping=True, future=True)
    # Relocate any pre-``reddit`` tables (including the version table) first, in
    # its own committed transaction, so Alembic then reads migration state from
    # the version table in its new home instead of re-running from base.
    with engine.begin() as connection:
        _consolidate_into_schema(connection)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=VERSION_TABLE,
            version_table_schema=SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
