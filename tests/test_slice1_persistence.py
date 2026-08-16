"""Persistence contracts for source items, distillations, runs, and cursors."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.domain import DistillationRecord, ProcessState, RedditItem, RedditKind


def _distillation(fullname: str = "t3_abc123") -> DistillationRecord:
    return DistillationRecord(
        reddit_fullname=fullname,
        request_id=f"req-{fullname}",
        request={"source_item_id": fullname, "text": "input"},
        response={"distillation": {"summary": "output"}},
        created_at=datetime.now(timezone.utc),
    )


class TestDomainModels:
    def test_reddit_item_json_round_trip(self, make_item):
        item = make_item()
        restored = RedditItem.model_validate_json(item.model_dump_json())
        assert restored.fullname == item.fullname
        assert restored.kind is RedditKind.post
        assert restored.process_state is ProcessState.new

    def test_naive_datetime_coerced_to_utc(self, make_item):
        item = make_item(created_utc=datetime(2026, 1, 1, 12, 0, 0))
        assert item.created_utc.utcoffset().total_seconds() == 0


class TestRedditItems:
    def test_insert_get_and_dedup(self, repo, make_item):
        stored, duplicate = repo.insert_item(make_item(fullname="t3_dup", score=1))
        assert duplicate is False
        assert stored.score == 1

        stored, duplicate = repo.insert_item(make_item(fullname="t3_dup", score=999))
        assert duplicate is True
        assert stored.score == 1
        assert repo.get_item("t3_dup").created_utc.tzinfo is not None

    def test_set_state_and_list_by_state(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_a"))
        repo.insert_item(make_item(fullname="t3_b"))
        repo.set_item_state("t3_a", ProcessState.distilled)
        assert {item.fullname for item in repo.list_items_by_state("new")} == {"t3_b"}
        assert {item.fullname for item in repo.list_items_by_state("distilled")} == {"t3_a"}


class TestDistillations:
    def test_round_trip_and_dedup(self, repo, make_item):
        repo.insert_item(make_item())
        stored, duplicate = repo.insert_distillation(_distillation())
        assert duplicate is False
        assert stored.response["distillation"]["summary"] == "output"

        stored_again, duplicate = repo.insert_distillation(_distillation())
        assert duplicate is True
        assert stored_again.request_id == "req-t3_abc123"

    def test_list_and_summary_lookup(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_a"))
        repo.insert_distillation(_distillation("t3_a"))

        records, total = repo.list_distillations(request_id="req-t3_a")
        assert total == 1
        assert records[0].reddit_fullname == "t3_a"
        assert repo.latest_distillation_summaries(["t3_a"])["t3_a"][
            "distillation"
        ]["summary"] == "output"


class TestIngestCursor:
    def test_get_missing_then_upsert(self, repo):
        assert repo.get_cursor("wsb:new") is None
        repo.upsert_cursor("wsb:new", last_fullname="t3_a")
        repo.upsert_cursor("wsb:new", last_fullname="t3_b")
        assert repo.get_cursor("wsb:new").last_fullname == "t3_b"


class TestOperational:
    def test_stats_empty(self, repo):
        stats = repo.stats()
        assert stats["items_ingested"] == 0
        assert stats["distillations"] == 0
        assert stats["items_by_state"] == {
            "new": 0,
            "submitted": 0,
            "distilled": 0,
            "skipped": 0,
            "failed": 0,
        }

    def test_stats_counts(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_a"))
        repo.insert_item(make_item(fullname="t3_b"))
        repo.set_item_state("t3_b", ProcessState.distilled)
        repo.insert_distillation(_distillation("t3_b"))

        stats = repo.stats()
        assert stats["items_ingested"] == 2
        assert stats["items_by_state"]["new"] == 1
        assert stats["items_by_state"]["distilled"] == 1
        assert stats["distillations"] == 1
        assert stats["last_fetched_at"] is not None
