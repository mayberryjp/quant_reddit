"""Slice 6: orchestration worker end-to-end with all externals stubbed."""

from __future__ import annotations

import json
from datetime import date

import httpx
import respx

from app.services.orchestrator import HEARTBEAT_KEY, run_cycle, run_forever
from app.services.reddit_client import RawPost
from app.services.sentiment_emitter import SentimentEmitter
from app.services.signal_emitter import SignalEmitter

SENTIMENT_BASE = "http://sentiment.test:8017"
SIGNALS_BASE = "http://signals.test:8016"
DAY = date(2026, 7, 6)

GME_FINDINGS = json.dumps(
    {
        "findings": [
            {
                "ticker": "GME",
                "sentiment_score": 80,
                "direction": "long",
                "confidence": 0.9,
                "is_watchlist_candidate": True,
                "rationale": "squeeze",
            }
        ]
    }
)


class FakeSource:
    def __init__(self, posts, comments=None):
        self._posts = list(posts)
        self._comments = comments or {}

    def new_posts(self, subreddit, limit):
        return list(self._posts[:limit])

    def post_comments(self, post_id, limit):
        return list(self._comments.get(post_id, [])[:limit])


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def chat(self, system, user):
        self.calls += 1
        return self.response


def _post(pid, *, body="$GME to the moon", score=5, created=1_700_000_000.0) -> RawPost:
    return RawPost(
        fullname=f"t3_{pid}",
        id=pid,
        title="GME thread",
        body=body,
        author="u",
        score=score,
        permalink=f"/r/wsb/{pid}",
        created_utc=created,
        num_comments=0,
    )


def _emitters(repo):
    se = SentimentEmitter(
        repo, base_url=SENTIMENT_BASE, source="reddit-wsb-v1", source_weight=0.5, retries=1, backoff=0
    )
    sig = SignalEmitter(
        repo,
        base_url=SIGNALS_BASE,
        source="reddit-wsb-v1",
        min_mentions=3,
        watchlist_min_score=0.5,
        retries=1,
        backoff=0,
    )
    return se, sig


class TestFullCycle:
    @respx.mock
    def test_cycle_then_idempotent_rerun(self, repo):
        respx.post(f"{SENTIMENT_BASE}/sentiment").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "sentiment_id": "obs"})
        )
        respx.post(f"{SIGNALS_BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "signal_cache_id": "sig"})
        )
        source = FakeSource(posts=[_post(f"p{i}") for i in range(4)])
        llm = FakeLLM(GME_FINDINGS)
        se, sig = _emitters(repo)

        r1 = run_cycle(
            repo,
            reddit_source=source,
            llm_client=llm,
            sentiment_emitter=se,
            signal_emitter=sig,
            subreddit="wallstreetbets",
            day=DAY,
        )
        assert r1.ingest.posts_new == 4
        assert r1.items_distilled == 4
        assert r1.findings == 4
        assert r1.sentiment_emitted == 4
        assert r1.signals_emitted == 1

        stats = repo.stats()
        assert stats["emissions"]["sentiment"]["accepted"] == 4
        assert stats["emissions"]["signals"]["accepted"] == 1
        assert stats["items_by_state"]["distilled"] == 4
        assert repo.get_cursor(HEARTBEAT_KEY) is not None

        # Re-run with the same source: ingest yields only duplicates; nothing new.
        calls_before = llm.calls
        r2 = run_cycle(
            repo,
            reddit_source=source,
            llm_client=llm,
            sentiment_emitter=se,
            signal_emitter=sig,
            subreddit="wallstreetbets",
            day=DAY,
        )
        assert r2.ingest.posts_new == 0
        assert r2.ingest.posts_duplicate == 4
        assert r2.items_distilled == 0
        assert r2.findings == 0
        assert r2.sentiment_emitted == 0
        assert r2.signals_emitted == 0
        assert llm.calls == calls_before  # no new LLM calls on re-run

        stats2 = repo.stats()
        assert stats2["emissions"]["sentiment"]["accepted"] == 4  # unchanged
        assert stats2["emissions"]["signals"]["accepted"] == 1  # unchanged

    @respx.mock
    def test_below_threshold_no_signal(self, repo):
        respx.post(f"{SENTIMENT_BASE}/sentiment").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "sentiment_id": "obs"})
        )
        signals_route = respx.post(f"{SIGNALS_BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "signal_cache_id": "sig"})
        )
        # Only 2 mentions -> below min_mentions=3: sentiment still emitted, no signal.
        source = FakeSource(posts=[_post(f"p{i}") for i in range(2)])
        se, sig = _emitters(repo)
        r = run_cycle(
            repo,
            reddit_source=source,
            llm_client=FakeLLM(GME_FINDINGS),
            sentiment_emitter=se,
            signal_emitter=sig,
            subreddit="wallstreetbets",
            day=DAY,
        )
        assert r.sentiment_emitted == 2
        assert r.signals_emitted == 0
        assert signals_route.called is False


class TestRunForever:
    @respx.mock
    def test_run_once_drives_a_cycle(self, repo):
        respx.post(f"{SENTIMENT_BASE}/sentiment").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "sentiment_id": "obs"})
        )
        respx.post(f"{SIGNALS_BASE}/signals").mock(
            return_value=httpx.Response(201, json={"status": "accepted", "signal_cache_id": "sig"})
        )
        source = FakeSource(posts=[_post(f"p{i}") for i in range(3)])
        se, sig = _emitters(repo)
        run_forever(
            repo,
            reddit_source=source,
            llm_client=FakeLLM(GME_FINDINGS),
            sentiment_emitter=se,
            signal_emitter=sig,
            run_once=True,
            subreddit="wallstreetbets",
            day=DAY,
        )
        assert repo.stats()["items_ingested"] == 3
        assert repo.get_cursor(HEARTBEAT_KEY) is not None
