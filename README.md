# quant_reddit

`quant_reddit` is a source worker for the quant platform. It discovers Reddit posts
and comments, persists the source records, submits each new item to the shared
`quant_distill` `POST /v1/process` endpoint, and stores the exact request and complete
response. Artifact generation and downstream delivery are owned by `quant_distill`.

## Quick Start

```bash
pip install -e ".[dev]"
playwright install chromium
pytest -v

# API on :8018 and PostgreSQL on :5432
docker compose up --build
```

Set `QUANT_DISTILL_URL` to the distillation service base URL. Its default is
`http://localhost:8021` outside Docker and `http://host.docker.internal:8021` in
the included Compose example.

## Flow

```text
Reddit -> reddit_items -> POST quant_distill /v1/process -> distillations
```

Supervisord runs separate ingestion, processing, and Bottle API processes. The
processing worker uses bounded retries for network failures and HTTP `503`. An item
is marked `distilled` only after its request and successful response are persisted.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/reddit/health` | Process liveness |
| GET | `/reddit/ready` | Database readiness |
| GET | `/reddit/stats` | Ingestion and distillation counters |
| GET | `/reddit/items/recent` | Source records with filters and pagination |
| GET | `/reddit/distillations/recent` | Stored requests and authoritative responses |
| GET | `/reddit/runs/recent` | Ingest and process run history |

## Configuration

- `DATABASE_URL`: PostgreSQL DSN.
- `QUANT_DISTILL_URL`: `quant_distill` base URL; the client appends `/v1/process`.
- `QUANT_REDDIT_DISTILL_TIMEOUT`: request timeout in seconds, default `180`.
- `QUANT_REDDIT_HTTP_RETRIES`: total bounded attempts, default `3`.
- `REDDIT_SOURCE_MODE`: `auto`, `praw`, or `scrape`.
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`: PRAW settings.
- `QUANT_REDDIT_INGEST_INTERVAL`, `QUANT_REDDIT_PROCESS_INTERVAL`: worker intervals.
- `QUANT_REDDIT_POST_MIN_CHARS`, `QUANT_REDDIT_POST_MAX_CHARS`: source text limits.

See [architecture](docs/architecture.md), [request mapping](docs/producer_mapping.md),
and the [runbook](docs/runbook.md).
