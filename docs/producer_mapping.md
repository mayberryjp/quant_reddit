# Producer Mapping

How `quant_reddit` maps distilled findings onto the two downstream contracts. It
records every attempt in `emission_log` and is idempotent per key.

## → `quant_signals` `POST /signals` (watchlist candidate)

Emitted per ticker per day, only when
`mention_count ≥ QUANT_REDDIT_MIN_MENTIONS` **and**
`score ≥ QUANT_REDDIT_WATCHLIST_MIN_SCORE`.

| Signal field | Value |
|---|---|
| `source` | `QUANT_REDDIT_SIGNAL_SOURCE` (`reddit-wsb-v1`) |
| `idempotency_key` | `{source}:{yyyy-mm-dd}:{TICKER}` (one candidate per ticker per day) |
| `ticker` | Extracted ticker (uppercased) |
| `signal_type` | `watchlist_candidate` |
| `direction` | `long`/`short`/`neutral` from aggregate sentiment vs neutral band |
| `score` | Normalized conviction `[0,1]` = f(mention volume, `|sentiment|/100`, confidence) |
| `confidence` | Mean LLM confidence `[0,1]` |
| `reason` | Mention summary + top rationale (≤ 2000) |
| `tags` | `["wallstreetbets","reddit","llm"]` |
| `metadata` | `{reddit_fullnames, mention_count, model, prompt_version, window}` |

`quant_signals` always responds `201`; the outcome is in the body
(`status` ∈ `accepted` / `duplicate` / `unresolved`) and is recorded accordingly.

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
