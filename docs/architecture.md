# Architecture

`quant_reddit` is a **producer** service: it reads r/wallstreetbets, distills
structured investment signals with a local Ollama LLM, and emits watchlist signals
and sentiment observations to sibling platform services. It owns an audit +
idempotency ledger, not a system of record.

## Pipeline

```
ingest (Reddit)  →  distill (Ollama)  →  emit (signals + sentiment)
```

A supervisord-managed **ingest worker** runs Reddit acquisition on
`QUANT_REDDIT_INGEST_INTERVAL`, and a separate **process worker** runs
`distill → emit` on `QUANT_REDDIT_PROCESS_INTERVAL`; a sibling **API** process
(Bottle + waitress) serves health/readiness/stats and read endpoints.
All share one PostgreSQL database.

```mermaid
flowchart LR
  WSB[r/wallstreetbets, etc.] -->|OAuth read (PRAW) or browser scrape| ING[ingest worker]
  ING --> DB[(PostgreSQL: reddit schema)]
  DB --> DIST[process worker: distiller + OpenAI-compatible /v1/chat/completions]
  DIST --> DB
  DB --> EMIT[emitters]
  EMIT -->|POST /signals| SIG[quant_signals :8016]
  EMIT -->|POST /sentiment| SEN[quant_sentiment :8017]
  API[Bottle API :8018] --- DB
```

## Components

| Module | Responsibility |
|---|---|
| `app/services/reddit_client.py` | PRAW script-app source + `ingest_once` (idempotent persist, cursor, selective comments) |
| `app/services/ollama_client.py` | httpx client for `POST {OLLAMA_BASE_URL}/chat/completions` (`response_format=json_object`) |
| `app/services/distiller.py` | versioned, prompt-injection-safe distillation → validated `TickerFinding`s |
| `app/services/sentiment_emitter.py` | one observation per (item, ticker) → `quant_sentiment` |
| `app/services/signal_emitter.py` | per-finding watchlist submission with version-scoped idempotency parity to `quant_signals` |
| `app/services/orchestrator.py` | ingest-only/process-only loops and combined compatibility loop |
| `app/ingest_worker.py` | ingest-only process entrypoint |
| `app/process_worker.py` | process-only entrypoint (`new` items → LLM → emissions) |
| `app/repository/` | SQLAlchemy Core schema + repository (dedup, state, emissions, cursor, stats) |
| `app/routes/` | `health.py` (health/ready/stats), `reddit.py` (recent read endpoints) |

## Data model (`reddit` schema)

Append-mostly ledger. See [`app/repository/schema.py`](../app/repository/schema.py):

- `reddit_items` — raw posts/comments, unique by `fullname`; `process_state`
  transitions `new → distilled | skipped | failed`.
- `llm_extractions` — structured LLM output, unique by `(reddit_fullname, model, prompt_version)`.
- `emission_log` — every downstream POST attempt, unique by `(target, idempotency_key)`.
- `ingest_cursor` — per-source watermark (also stores the worker heartbeat).

## Design principles

- **Graceful degradation** — a failure ingesting/distilling/emitting one item never
  aborts the batch; failures are counted and the rest proceed.
- **Idempotency everywhere** — re-running a cycle re-ingests only duplicates, never
  re-calls the LLM for already-distilled items, and never re-POSTs delivered emissions.
- **Prompt-injection safety** — untrusted Reddit text is passed to the model as
  clearly delimited *data*, never as instructions; the model is constrained to JSON;
  every field is validated with pydantic. No model output is ever `eval`'d or used to
  build shell/SQL.
