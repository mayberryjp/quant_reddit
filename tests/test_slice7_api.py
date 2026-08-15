"""Read and operational API tests."""

from __future__ import annotations

from datetime import datetime, timezone

from app import dependencies
from app.models.domain import DistillationRecord, ProcessState, RedditKind


class _DownRepo:
    def ping(self) -> bool:
        return False


def _record(fullname: str) -> DistillationRecord:
    return DistillationRecord(
        reddit_fullname=fullname,
        request_id=f"req-{fullname}",
        request={"source_item_id": fullname, "text": "input"},
        response={
            "status": "ok",
            "request_id": f"req-{fullname}",
            "distillation": {"summary": "Short summary"},
            "sentiment": {"observations": [{"subject": "ABC"}]},
            "entities": {"items": [{"ticker": "ABC"}]},
        },
        created_at=datetime.now(timezone.utc),
    )


class TestReady:
    def test_ready_ok(self, app_client):
        response = app_client.get("/reddit/ready")
        assert response.json["status"] == "ready"

    def test_ready_503_when_db_down(self, app_client):
        dependencies.set_repo(_DownRepo())
        response = app_client.get("/reddit/ready", expect_errors=True)
        assert response.status_int == 503
        assert response.json["database"] == "unavailable"


class TestStats:
    def test_stats_empty(self, app_client):
        response = app_client.get("/reddit/stats")
        assert response.json["items_ingested"] == 0
        assert response.json["distillations"] == 0
        assert response.json["last_run"] is None

    def test_stats_after_activity(self, app_client, repo, make_item):
        repo.insert_item(make_item(fullname="t3_a"))
        repo.set_item_state("t3_a", ProcessState.distilled)
        repo.insert_distillation(_record("t3_a"))
        repo.set_heartbeat()

        response = app_client.get("/reddit/stats")
        assert response.json["items_by_state"]["distilled"] == 1
        assert response.json["distillations"] == 1
        assert response.json["last_run"] is not None


class TestItemsRecent:
    def test_lists_filters_and_paginates(self, app_client, repo, make_item):
        repo.insert_item(make_item(fullname="t3_p", kind=RedditKind.post))
        repo.insert_item(make_item(fullname="t1_c", kind=RedditKind.comment))
        repo.set_item_state("t1_c", ProcessState.distilled)

        response = app_client.get("/reddit/items/recent", {"kind": "comment"})
        assert response.json["total"] == 1
        assert response.json["items"][0]["fullname"] == "t1_c"

    def test_includes_summary_and_char_counts(self, app_client, repo, make_item):
        repo.insert_item(make_item(fullname="t3_summary", title="ABC", body="def ghi"))
        repo.insert_distillation(_record("t3_summary"))

        response = app_client.get(
            "/reddit/items/recent",
            {"include_summary": "1", "include_char_counts": "1"},
        )
        item = response.json["items"][0]
        assert item["summary_text"] == "Short summary"
        assert item["title_chars"] == 3
        assert item["body_chars"] == 7
        assert item["content_chars"] == 11
        assert item["summary_chars"] == 13


class TestDistillationsRecent:
    def test_lists_and_filters(self, app_client, repo, make_item):
        repo.insert_item(make_item(fullname="t3_x"))
        repo.insert_distillation(_record("t3_x"))

        response = app_client.get(
            "/reddit/distillations/recent", {"request_id": "req-t3_x"}
        )
        assert response.json["total"] == 1
        record = response.json["items"][0]
        assert record["reddit_fullname"] == "t3_x"
        assert record["response"]["entities"]["items"][0]["ticker"] == "ABC"
