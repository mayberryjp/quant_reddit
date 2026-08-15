# quant_distill Request Mapping

Every new Reddit item is sent to `POST {QUANT_DISTILL_URL}/v1/process`.

| Request field | Reddit value |
|---|---|
| `source` | `quant_reddit` |
| `source_type` | `reddit` |
| `source_item_id` | `reddit_items.fullname` |
| `title` | Item title, when present |
| `text` | Item body, falling back to title when the body is empty |
| `observed_at` | Reddit `created_utc` timestamp |
| `metadata.kind` | `post` or `comment` |
| `metadata.subreddit` | Source subreddit |
| `metadata.author` | Reddit author, when available |
| `metadata.permalink` | Reddit permalink, when available |
| `metadata.parent_fullname` | Parent item, when available |
| `metadata.score` | Reddit score at ingestion |

Options are omitted so `quant_distill` controls its documented defaults. This worker
does not inspect, transform, or separately deliver generated artifacts. It stores the
entire successful response in `reddit.distillations.response` and the submitted body
in `reddit.distillations.request`.
