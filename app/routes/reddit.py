"""Read endpoints for the reddit ledger (Slice 7).

``/reddit/items/recent``, ``/reddit/extractions/recent`` and
``/reddit/emissions/recent`` return newest-first pages with optional filters.
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


@sub.get("/reddit/items/recent")
def items_recent():
    page, page_size = _page_params()
    items, total = get_repo().list_items(
        kind=_param("kind"),
        process_state=_param("process_state"),
        subreddit=_param("subreddit"),
        page=page,
        page_size=page_size,
    )
    return {
        "items": [i.model_dump(mode="json") for i in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@sub.get("/reddit/extractions/recent")
def extractions_recent():
    page, page_size = _page_params()
    items, total = get_repo().list_extractions(
        model=_param("model"),
        prompt_version=_param("prompt_version"),
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


@sub.get("/reddit/emissions/recent")
def emissions_recent():
    page, page_size = _page_params()
    items, total = get_repo().list_emissions(
        target=_param("target"),
        status=_param("status"),
        ticker=_param("ticker"),
        page=page,
        page_size=page_size,
    )
    return {
        "items": [r.model_dump(mode="json") for r in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
