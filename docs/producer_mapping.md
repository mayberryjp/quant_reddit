# Producer Mapping

How `quant_reddit` maps distilled findings onto the two downstream contracts. It
records every attempt in `emission_log` and is idempotent per key.

## → `quant_signals` `POST /signals` (strict parity mode)

Emitted once per resolved finding (per item+ticker), with version-scoped
idempotency parity.

| Signal field | Value |
|---|---|
| `source` | `QUANT_REDDIT_SIGNAL_SOURCE` (`reddit-wsb-v1`) |
| `idempotency_key` | `{source}:{reddit_fullname}:{TICKER}:{model}:{prompt_version}` |
| `ticker` | Extracted ticker (uppercased) |
| `signal_type` | `cnbc_mention` (default; configurable) |
| `direction` | `long`/`short`/`neutral` from the finding |
| `confidence` | LLM confidence `[0,1]` |
| `reason` | Finding rationale (≤ 2000) |
| `tags` | `["reddit","llm"]` |
| `metadata` | `{reddit_fullname, model, prompt_version, window}` |

Status classification is HTTP-based: `201` accepted, `200` duplicate, otherwise failed.

## → `quant_sentiment` `POST /sentiment`

One observation per (item, ticker). `sentiment_label` is **never** sent — it is
derived server-side.

| Sentiment field | Value |
|---|---|
| `source` | `QUANT_REDDIT_SENTIMENT_SOURCE` (`reddit-wsb-v1`) |
| `idempotency_key` | `{source}:{reddit_fullname}:{TICKER}` |
| `subject_type` | `ticker` |
| `subject` | Extracted ticker |
| `sentiment_score` | LLM score on `[-100, 100]` |
| `confidence` | LLM confidence `[0,1]` |
| `source_weight` | `QUANT_REDDIT_SOURCE_WEIGHT` producer reliability weight `[0,1]` |
| `reason` | LLM rationale (≤ 2000) |
| `observed_at` | Reddit item `created_utc` (ISO-8601) |
| `tags` | `["wallstreetbets","reddit"]` |
| `metadata` | `{reddit_fullname, permalink, model, prompt_version}` |

`quant_sentiment` returns `201` (accepted) / `200` (duplicate); both are recorded.
