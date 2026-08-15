# quant_reddit Worker Specification

## Purpose

`quant_reddit` acquires Reddit source data and submits it to the shared
`quant_distill` API. It owns source discovery, source persistence, processing run
history, and persistence of the exact distillation request and response.

## Required Behavior

1. Ingest posts and selected comments idempotently by Reddit fullname.
2. Process only items in the `new` state.
3. Send each item to `POST {QUANT_DISTILL_URL}/v1/process` with stable source identity,
   source text, observation time, and non-secret Reddit metadata.
4. Retry network failures and HTTP `503` with a bounded policy. Do not retry an
   unchanged HTTP `422` request.
5. Persist the exact submitted JSON and complete successful JSON response.
6. Mark an item `distilled` only after persistence succeeds; mark exhausted or invalid
   processing attempts `failed`.
7. Persist ingest and process cycle results independently.
8. Do not perform local LLM processing or direct artifact delivery.

## Data Model

- `reddit_items(fullname UNIQUE, source fields, process_state, timestamps)`
- `distillations(reddit_fullname UNIQUE, request_id, request JSONB, response JSONB,
  created_at, schema_version)`
- `ingest_cursor(source_key PRIMARY KEY, watermark fields, updated_at)`
- `cycle_runs(run_type, started_at, finished_at, result JSONB, error)`

## Operational Contract

The service provides liveness, database readiness, aggregate stats, paginated source
records, paginated distillation records, and paginated cycle-run records under
`/reddit/*`. PostgreSQL is used in production and SQLite schema translation in tests.
