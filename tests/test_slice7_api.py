"""Slice 7: read/ops API — health, ready (incl. 503), stats, recent endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from app import dependencies
from app.models.domain import (
    EmissionStatus,
    EmissionTarget,
    LlmExtraction,
    ProcessState,
    RedditKind,
    TickerFinding,
)


class _DownRepo:
    def ping(self) -> bool:
        return False


class TestReady:
    def test_ready_ok(self, app_client):
        r = app_client.get("/reddit/ready")
        assert r.status_int == 200
        assert r.json["status"] == "ready"
        assert r.json["database"] == "ok"

    def test_ready_503_when_db_down(self, app_client):
        dependencies.set_repo(_DownRepo())
        r = app_client.get("/reddit/ready", expect_errors=True)
        assert r.status_int == 503
        assert r.json["status"] == "not_ready"
        assert r.json["database"] == "unavailable"


class TestStats:
    def test_stats_empty(self, app_client):
        r = app_client.get("/reddit/stats")
        assert r.status_int == 200
        assert r.json["items_ingested"] == 0
        assert r.json["extractions"] == 0
        assert r.json["emissions"]["signals"]["accepted"] == 0
        assert r.json["emissions"]["sentiment"]["accepted"] == 0
        assert r.json["last_fetched_at"] is None
        assert r.json["last_run"] is None

    def test_stats_after_activity(self, app_client, repo, make_item):
        repo.insert_item(make_item(fullname="t3_a"))
        repo.insert_item(make_item(fullname="t3_b"))
        repo.set_item_state("t3_b", ProcessState.distilled)
        repo.insert_extraction(
            LlmExtraction(
                reddit_fullname="t3_b",
                model="llama3.1",
                prompt_version="wsb-distill-v1",
                extracted=[TickerFinding(ticker="GME", sentiment_score=50)],
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.record_emission(
            target=EmissionTarget.signals,
            idempotency_key="reddit-wsb-v1:2026-07-06:GME",
            status=EmissionStatus.accepted,
            ticker="GME",
        )
        repo.set_heartbeat()

        r = app_client.get("/reddit/stats")
        assert r.json["items_ingested"] == 2
        assert r.json["items_by_state"]["distilled"] == 1
        assert r.json["extractions"] == 1
        assert r.json["emissions"]["signals"]["accepted"] == 1
        assert r.json["last_fetched_at"] is not None
        assert r.json["last_run"] is not None


class TestItemsRecent:
    def test_empty(self, app_client):
        r = app_client.get("/reddit/items/recent")
        assert r.status_int == 200
        assert r.json["items"] == []
        assert r.json["total"] == 0

    def test_lists_and_filters(self, app_client, repo, make_item):
        repo.insert_item(make_item(fullname="t3_p", kind=RedditKind.post))
        repo.insert_item(make_item(fullname="t1_c", kind=RedditKind.comment))
        repo.set_item_state("t1_c", ProcessState.distilled)

        r = app_client.get("/reddit/items/recent")
        assert r.json["total"] == 2

        r = app_client.get("/reddit/items/recent", {"kind": "comment"})
        assert r.json["total"] == 1
        assert r.json["items"][0]["fullname"] == "t1_c"

        r = app_client.get("/reddit/items/recent", {"process_state": "distilled"})
        assert r.json["total"] == 1

    def test_pagination(self, app_client, repo, make_item):
        for i in range(5):
            repo.insert_item(make_item(fullname=f"t3_{i}"))
        r = app_client.get("/reddit/items/recent", {"page": 1, "page_size": 2})
        assert len(r.json["items"]) == 2
        assert r.json["total"] == 5

    def test_invalid_page_size_422(self, app_client):
        r = app_client.get(
            "/reddit/items/recent", {"page_size": "abc"}, expect_errors=True
        )
        assert r.status_int == 422
        assert "detail" in r.json


class TestExtractionsRecent:
    def test_lists(self, app_client, repo, make_item):
        repo.insert_item(make_item(fullname="t3_x"))
        repo.insert_extraction(
            LlmExtraction(
                reddit_fullname="t3_x",
                model="llama3.1",
                prompt_version="wsb-distill-v1",
                extracted=[TickerFinding(ticker="GME", sentiment_score=50)],
                created_at=datetime.now(timezone.utc),
            )
        )
        r = app_client.get("/reddit/extractions/recent")
        assert r.json["total"] == 1
        assert r.json["items"][0]["reddit_fullname"] == "t3_x"
        assert r.json["items"][0]["extracted"][0]["ticker"] == "GME"


class TestEmissionsRecent:
    def test_lists_and_filters(self, app_client, repo):
        repo.record_emission(
            target=EmissionTarget.signals,
            idempotency_key="reddit-wsb-v1:2026-07-06:GME",
            status=EmissionStatus.accepted,
            ticker="GME",
        )
        repo.record_emission(
            target=EmissionTarget.sentiment,
            idempotency_key="reddit-wsb-v1:t3_a:AMC",
            status=EmissionStatus.failed,
            ticker="AMC",
        )
        r = app_client.get("/reddit/emissions/recent")
        assert r.json["total"] == 2

        r = app_client.get("/reddit/emissions/recent", {"target": "signals"})
        assert r.json["total"] == 1
        assert r.json["items"][0]["ticker"] == "GME"

        r = app_client.get("/reddit/emissions/recent", {"status": "failed"})
        assert r.json["total"] == 1
        assert r.json["items"][0]["ticker"] == "AMC"
