"""Read endpoints for the reddit ledger (Slice 7).

``/reddit/items/recent`` and ``/reddit/distillations/recent`` return newest-first
pages with optional filters.
"""

from __future__ import annotations

from bottle import Bottle, HTTPResponse, request

from app.config import settings
from app.dependencies import get_repo

sub = Bottle()


def _error(status: int, detail: str) -> HTTPResponse:
    import json

    return HTTPResponse(
        status=status,
        body=json.dumps({"detail": detail}),
        content_type="application/json",
    )


def _page_params() -> tuple[int, int]:
    params = request.params
    try:
        page = max(int(params.get("page", 1)), 1)
        page_size = int(params.get("page_size", settings.default_page_size))
    except (ValueError, TypeError):
        raise _error(422, "page and page_size must be integers")
    page_size = max(1, min(page_size, settings.max_page_size))
    return page, page_size


def _param(name: str) -> str | None:
    return request.params.get(name) or None


def _flag(name: str) -> bool:
    value = (request.params.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _extract_summary_text(api_response: dict | None) -> str | None:
    if not isinstance(api_response, dict):
        return None
    summary_payload = api_response.get("distillation")
    if isinstance(summary_payload, dict):
        summary_text = summary_payload.get("summary")
        if isinstance(summary_text, str) and summary_text.strip():
            return summary_text
    return None


def _char_counts(title: str | None, body: str | None, summary: str | None) -> dict[str, int]:
    title_text = title or ""
    body_text = body or ""
    if title_text and body_text:
        content_text = f"{title_text}\n{body_text}"
    else:
        content_text = title_text or body_text
    summary_text = summary or ""
    return {
        "title_chars": len(title_text),
        "body_chars": len(body_text),
        "content_chars": len(content_text),
        "summary_chars": len(summary_text),
    }


_ITEM_KINDS = {"post", "comment"}
_PROCESS_STATES = {"new", "submitted", "distilled", "skipped", "failed"}
_RUN_TYPES = {"ingest", "process", "full"}


def _choice(name: str, allowed: set[str]) -> str | None:
    value = request.params.get(name) or None
    if value is not None and value not in allowed:
        raise _error(422, f"invalid {name}; allowed: {', '.join(sorted(allowed))}")
    return value


@sub.get("/reddit/items/recent")
def items_recent():
    page, page_size = _page_params()
    include_summary = _flag("include_summary")
    include_char_counts = _flag("include_char_counts")
    items, total = get_repo().list_items(
        kind=_choice("kind", _ITEM_KINDS),
        process_state=_choice("process_state", _PROCESS_STATES),
        subreddit=_param("subreddit"),
        page=page,
        page_size=page_size,
    )
    payload_items = [i.model_dump(mode="json") for i in items]

    if include_summary or include_char_counts:
        repo = get_repo()
        fullnames = [i.fullname for i in items]
        summaries = repo.latest_distillation_summaries(fullnames)
        for item_model, payload in zip(items, payload_items):
            summary_text = _extract_summary_text(summaries.get(item_model.fullname))
            if include_summary:
                payload["summary_text"] = summary_text
            if include_char_counts:
                payload.update(
                    _char_counts(item_model.title, item_model.body, summary_text)
                )

    return {
        "items": payload_items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@sub.get("/reddit/distillations/recent")
def distillations_recent():
    page, page_size = _page_params()
    items, total = get_repo().list_distillations(
        request_id=_param("request_id"),
        reddit_fullname=_param("reddit_fullname"),
        page=page,
        page_size=page_size,
    )
    return {
        "items": [e.model_dump(mode="json") for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@sub.get("/reddit/runs/recent")
def runs_recent():
    page, page_size = _page_params()
    items, total = get_repo().list_cycle_runs(
        run_type=_choice("run_type", _RUN_TYPES),
        page=page,
        page_size=page_size,
    )
    return {
        "items": [r.model_dump(mode="json") for r in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
