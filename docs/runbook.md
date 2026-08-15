# Operational Runbook

## Start

```bash
export QUANT_DISTILL_URL=http://localhost:8021
docker compose up --build
```

The container applies Alembic migrations before supervisord starts the API and two
workers.

## Inspect

```bash
curl http://localhost:8018/reddit/health
curl http://localhost:8018/reddit/ready
curl http://localhost:8018/reddit/stats
curl "http://localhost:8018/reddit/items/recent?process_state=distilled"
curl "http://localhost:8018/reddit/distillations/recent?page=1&page_size=25"
curl "http://localhost:8018/reddit/runs/recent?run_type=process"
```

`/reddit/stats` reports item counts by state, total stored distillations, the latest
fetch time, and worker heartbeat.

## Failure Handling

- Network failures and HTTP `503` are retried with bounded exponential backoff.
- HTTP `422` and other non-`503` responses are not retried unchanged.
- Exhausted or invalid requests mark the item `failed`; no distillation row is stored.
- A successful item is marked `distilled` only after both request and response are
  committed.
- `processing.warnings` remain in the stored authoritative response for inspection.

## Validate

```bash
alembic upgrade head
pytest -v
```

Tests use in-memory SQLite and mocked Reddit/distillation HTTP boundaries.
