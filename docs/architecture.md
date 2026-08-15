# Architecture

`quant_reddit` owns Reddit discovery and local audit persistence. Shared LLM
processing and any downstream artifact delivery are outside this service.

```mermaid
flowchart LR
  REDDIT[Reddit] --> INGEST[ingest worker]
  INGEST --> ITEMS[(reddit_items)]
  ITEMS --> PROCESS[process worker]
  PROCESS -->|POST /v1/process| DISTILL[quant_distill]
  DISTILL --> PROCESS
  PROCESS --> RESULTS[(distillations)]
  API[Bottle API :8018] --- ITEMS
  API --- RESULTS
  RUNS[(cycle_runs)] --- INGEST
  RUNS --- PROCESS
```

## Ownership

| Component | Responsibility |
|---|---|
| `reddit_client.py` | Reddit discovery, filtering, truncation, and idempotent ingestion |
| `distill_client.py` | Request mapping, HTTP retries, and response-envelope validation |
| `orchestrator.py` | Item state transitions and worker run loops |
| `repository/` | Source records, exact API requests/responses, cursors, and run history |
| `routes/` | Health, statistics, and audit reads |

## Persistence

- `reddit_items`: raw source content keyed by Reddit fullname.
- `distillations`: one exact `/v1/process` request and authoritative response per
  source item, including the upstream request ID.
- `ingest_cursor`: source watermarks and worker heartbeat.
- `cycle_runs`: ingest/process start, finish, result, and error records.

The pre-existing `llm_extractions` and `emission_log` tables are not used by current
code. Migration `0003_distillations` leaves them intact to avoid destructive loss of
historical audit data.
