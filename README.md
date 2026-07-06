# quant_reddit

A **producer** service for the quant/algo platform. `quant_reddit` continuously reads
posts and comments from Reddit's r/wallstreetbets, sends the text to a **local Ollama
LLM** to distill structured investment signals, and emits two kinds of output to existing
platform services:

1. **Watchlist signals** → [`quant_signals`](https://github.com/mayberryjp/quant_signals) via `POST /signals`.
2. **Sentiment observations** → [`quant_sentiment`](https://github.com/mayberryjp/quant_sentiment) via `POST /sentiment`.

It is a producer/aggregator only: it does **not** manage the watchlist lifecycle, does
**not** aggregate sentiment over time, and does **not** place trades. Its own datastore is
an audit/idempotency ledger, not a system of record.

## Quick Start

```bash
# Run the full stack (API on :8018, PostgreSQL on :5432). Migrations run on start.
docker compose up --build

# Run the test suite (in-memory SQLite; no Docker/Postgres/Ollama needed)
pip install -e ".[dev]"
pytest -v
```

## Architecture

```
ingest (Reddit) → distill (Ollama) → emit (signals + sentiment)
```

A supervisord-managed worker runs the `ingest → distill → emit` pipeline on a poll
interval; a sibling Bottle API process serves health/readiness/stats and read endpoints.
PostgreSQL holds an append-mostly audit + idempotency ledger.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/reddit/health` | Liveness (no DB dependency) |
| GET | `/reddit/ready` | Readiness (DB + dependency reachability) |
| GET | `/reddit/stats` | Operational counters |
| GET | `/reddit/items/recent` | Recent ingested items (filters, pagination) |
| GET | `/reddit/extractions/recent` | Recent LLM extractions |
| GET | `/reddit/emissions/recent` | Recent downstream emissions |

## Configuration

- `DATABASE_URL` — PostgreSQL DSN (required in production).
- `API_LISTEN_ADDRESS` (default `0.0.0.0`), `API_PORT` (default `8018`).
- `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT` — Reddit OAuth (script app).
- `OLLAMA_BASE_URL` (default `http://localhost:11434`), `OLLAMA_MODEL` (default `llama3.1`).
- `QUANT_SIGNALS_URL` (default `http://localhost:8016`), `QUANT_SENTIMENT_URL` (default `http://localhost:8017`).
- `QUANT_REDDIT_*` — tuning knobs (poll interval, batch sizes, thresholds, pagination).
  See [.env.example](.env.example).

## Documentation

- [Architecture](docs/architecture.md)
- [Producer Mapping](docs/producer_mapping.md)
- [Runbook](docs/runbook.md)