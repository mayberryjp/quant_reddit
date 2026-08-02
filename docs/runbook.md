# Operational Runbook

## Health / Readiness

```bash
curl http://localhost:8018/reddit/health
# {"status": "ok"}   (liveness; no DB dependency)

curl http://localhost:8018/reddit/ready
# {"status": "ready", "database": "ok"}   (503 if the database is unreachable)
```

## Stats

```bash
curl http://localhost:8018/reddit/stats
# items_ingested, items_by_state, extractions, emissions
# (signals/sentiment × accepted/duplicate/failed),
# last_fetched_at, last_run (worker heartbeat)
```

## Reading the ledger

```bash
curl "http://localhost:8018/reddit/items/recent?kind=post&process_state=distilled&page=1&page_size=25"
curl "http://localhost:8018/reddit/extractions/recent?model=llama3.1"
curl "http://localhost:8018/reddit/emissions/recent?target=signals&status=accepted"
```

## Running locally (Docker)

```bash
docker compose up --build
# API on :8018, PostgreSQL on :5432. Migrations run on container start.
# Point OLLAMA_BASE_URL / QUANT_SIGNALS_URL / QUANT_SENTIMENT_URL at your services.
```

## Running migrations manually

```bash
export DATABASE_URL=postgresql+psycopg://reddit:reddit@localhost:5432/reddit
alembic upgrade head
```

## Running tests

```bash
pip install -e ".[dev]"
pytest -v          # in-memory SQLite; no Docker/Postgres/Ollama/Reddit required
```

## Configuration & secrets

- Secrets (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_PASSWORD`) are read
  from the environment only and are **never logged**. See [.env.example](../.env.example).
- Reddit access uses an OAuth2 **script app** (password grant) via PRAW. Respect
  Reddit's ToS and the ~100 queries/min per-OAuth-client limit; the ingester polls
  `/new` and fetches comments selectively for high-signal posts.
- The worker validates configuration at startup and logs (non-fatal) warnings for
  missing/invalid values.

## Security

- **Prompt injection:** Reddit text is untrusted and passed to the model as
  delimited data, never as instructions; output is JSON-constrained and validated
  with pydantic; nothing is `eval`'d or used to build shell/SQL.
- **Downstream URLs** are validated to be `http(s)` at startup.

## Troubleshooting

- `ready = not_ready` / `database: unavailable` → PostgreSQL unreachable; check
  `DATABASE_URL` and the DB container.
- Read endpoint `422` → invalid filter value or non-integer `page`/`page_size`; the
  `detail` explains which.
- No signals emitted → no distilled ticker findings were produced for new items,
  or downstream `/signals` calls are failing; check `/reddit/stats`,
  `/reddit/extractions/recent`, and `/reddit/emissions/recent`.

## Known limitations (v1)

- No watchlist lifecycle or time-series sentiment aggregation (owned by the sibling
  services); no trading; no auth layer on the local API; no historical backfill.
