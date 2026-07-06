"""Slice 1: persistence contracts — domain models, repository, dedup, ledger."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.domain import (
    Direction,
    EmissionRecord,
    EmissionStatus,
    EmissionTarget,
    LlmExtraction,
    ProcessState,
    RedditItem,
    RedditKind,
    TickerFinding,
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
        assert item.created_utc.tzinfo is not None
        assert item.created_utc.utcoffset().total_seconds() == 0

    def test_ticker_finding_round_trip(self):
        tf = TickerFinding(
            ticker="GME",
            sentiment_score=75.0,
            direction=Direction.long,
            confidence=0.9,
            is_watchlist_candidate=True,
            rationale="squeeze",
        )
        restored = TickerFinding.model_validate_json(tf.model_dump_json())
        assert restored.ticker == "GME"
        assert restored.direction is Direction.long


class TestRedditItems:
    def test_insert_and_get(self, repo, make_item):
        item = make_item()
        stored, is_dup = repo.insert_item(item)
        assert is_dup is False
        fetched = repo.get_item(item.fullname)
        assert fetched is not None
        assert fetched.subreddit == "wallstreetbets"
        assert fetched.score == 4200
        assert fetched.created_utc.tzinfo is not None

    def test_dedup_on_fullname(self, repo, make_item):
        first = make_item(fullname="t3_dup", score=1)
        _, dup1 = repo.insert_item(first)
        assert dup1 is False
        second = make_item(fullname="t3_dup", score=999)
        stored, dup2 = repo.insert_item(second)
        assert dup2 is True
        assert stored.score == 1  # first write wins; never overwritten

    def test_get_missing(self, repo):
        assert repo.get_item("t3_nope") is None

    def test_set_state_and_list_by_state(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_a"))
        repo.insert_item(make_item(fullname="t3_b"))
        repo.set_item_state("t3_a", ProcessState.distilled)
        new_items = repo.list_items_by_state(ProcessState.new)
        distilled = repo.list_items_by_state("distilled")
        assert {i.fullname for i in new_items} == {"t3_b"}
        assert {i.fullname for i in distilled} == {"t3_a"}


class TestExtractions:
    def _extraction(self, **overrides) -> LlmExtraction:
        data = dict(
            reddit_fullname="t3_abc123",
            model="llama3.1",
            prompt_version="v1",
            raw_response={"raw": "..."},
            extracted=[
                TickerFinding(ticker="GME", sentiment_score=80, direction=Direction.long)
            ],
            created_at=datetime.now(timezone.utc),
        )
        data.update(overrides)
        return LlmExtraction(**data)

    def test_insert_and_get(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_abc123"))
        ex = self._extraction()
        stored, is_dup = repo.insert_extraction(ex)
        assert is_dup is False
        fetched = repo.get_extraction("t3_abc123", "llama3.1", "v1")
        assert fetched is not None
        assert len(fetched.extracted) == 1
        assert fetched.extracted[0].ticker == "GME"
        assert fetched.extracted[0].direction is Direction.long

    def test_dedup_on_key(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_abc123"))
        _, dup1 = repo.insert_extraction(self._extraction())
        assert dup1 is False
        _, dup2 = repo.insert_extraction(self._extraction(raw_response={"different": 1}))
        assert dup2 is True

    def test_different_prompt_version_is_new(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_abc123"))
        repo.insert_extraction(self._extraction(prompt_version="v1"))
        _, dup = repo.insert_extraction(self._extraction(prompt_version="v2"))
        assert dup is False


class TestEmissionLog:
    def test_record_insert_then_update_increments_attempts(self, repo):
        rec = repo.record_emission(
            target=EmissionTarget.sentiment,
            idempotency_key="reddit-wsb-v1:t3_abc123:GME",
            status=EmissionStatus.failed,
            ticker="GME",
            request={"subject": "GME"},
            http_status=500,
        )
        assert rec.attempts == 1
        assert rec.status is EmissionStatus.failed
        rec2 = repo.record_emission(
            target=EmissionTarget.sentiment,
            idempotency_key="reddit-wsb-v1:t3_abc123:GME",
            status=EmissionStatus.accepted,
            ticker="GME",
            request={"subject": "GME"},
            http_status=201,
            response_id="obs-1",
        )
        assert rec2.attempts == 2
        assert rec2.status is EmissionStatus.accepted
        assert rec2.response_id == "obs-1"

    def test_same_key_different_target_are_separate(self, repo):
        repo.record_emission(
            target=EmissionTarget.signals,
            idempotency_key="k",
            status=EmissionStatus.accepted,
        )
        repo.record_emission(
            target=EmissionTarget.sentiment,
            idempotency_key="k",
            status=EmissionStatus.accepted,
        )
        assert repo.get_emission(EmissionTarget.signals, "k").attempts == 1
        assert repo.get_emission(EmissionTarget.sentiment, "k").attempts == 1

    def test_get_missing(self, repo):
        assert repo.get_emission(EmissionTarget.signals, "nope") is None


class TestIngestCursor:
    def test_get_missing_then_upsert(self, repo):
        assert repo.get_cursor("wsb:new") is None
        cur = repo.upsert_cursor(
            "wsb:new",
            last_fullname="t3_abc123",
            last_created_utc=datetime(2026, 7, 6, tzinfo=timezone.utc),
        )
        assert cur.last_fullname == "t3_abc123"
        fetched = repo.get_cursor("wsb:new")
        assert fetched.last_fullname == "t3_abc123"

    def test_upsert_updates_existing(self, repo):
        repo.upsert_cursor("wsb:new", last_fullname="t3_a")
        repo.upsert_cursor("wsb:new", last_fullname="t3_b")
        assert repo.get_cursor("wsb:new").last_fullname == "t3_b"


class TestOperational:
    def test_ping(self, repo):
        assert repo.ping() is True

    def test_stats_empty(self, repo):
        s = repo.stats()
        assert s["items_ingested"] == 0
        assert s["extractions"] == 0
        assert s["items_by_state"] == {
            "new": 0,
            "distilled": 0,
            "skipped": 0,
            "failed": 0,
        }
        assert s["emissions"]["signals"]["accepted"] == 0
        assert s["last_fetched_at"] is None

    def test_stats_counts(self, repo, make_item):
        repo.insert_item(make_item(fullname="t3_a"))
        repo.insert_item(make_item(fullname="t3_b"))
        repo.set_item_state("t3_b", ProcessState.distilled)
        repo.insert_extraction(
            LlmExtraction(
                reddit_fullname="t3_b",
                model="llama3.1",
                prompt_version="v1",
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
        repo.record_emission(
            target=EmissionTarget.sentiment,
            idempotency_key="reddit-wsb-v1:t3_b:GME",
            status=EmissionStatus.duplicate,
            ticker="GME",
        )
        s = repo.stats()
        assert s["items_ingested"] == 2
        assert s["items_by_state"]["new"] == 1
        assert s["items_by_state"]["distilled"] == 1
        assert s["extractions"] == 1
        assert s["emissions"]["signals"]["accepted"] == 1
        assert s["emissions"]["sentiment"]["duplicate"] == 1
        assert s["last_fetched_at"] is not None
