"""Slice 2: Reddit ingestion — persistence, idempotency, cursor, selective comments.

No network is touched: a fake :class:`RedditSource` supplies normalized objects.
"""

from __future__ import annotations

from app.services.reddit_client import (
    RawComment,
    RawPost,
    has_ticker_mention,
    ingest_once,
    should_fetch_comments,
)

SUB = "wallstreetbets"
LONG_BODY = "x" * 900


def make_post(
    pid="p1",
    *,
    score=0,
    num_comments=0,
    title="daily discussion",
    body=LONG_BODY,
    created=1_700_000_000.0,
) -> RawPost:
    return RawPost(
        fullname=f"t3_{pid}",
        id=pid,
        title=title,
        body=body,
        author="wsb_user",
        score=score,
        permalink=f"https://www.reddit.com/r/wsb/{pid}",
        created_utc=created,
        num_comments=num_comments,
    )


def make_comment(cid="c1", *, parent="t3_p1", score=1, created=1_700_000_100.0) -> RawComment:
    return RawComment(
        fullname=f"t1_{cid}",
        body="to the moon",
        author="ape",
        score=score,
        permalink=f"https://www.reddit.com/r/wsb/{cid}",
        created_utc=created,
        parent_fullname=parent,
    )


class FakeRedditSource:
    def __init__(self, posts, comments=None, fail_comments_for=None):
        self._posts = list(posts)  # newest first
        self._comments = comments or {}
        self._fail = set(fail_comments_for or [])
        self.comment_calls: list[str] = []

    def new_posts(self, subreddit, limit):
        return list(self._posts[:limit])

    def post_comments(self, post_id, limit):
        self.comment_calls.append(post_id)
        if post_id in self._fail:
            raise RuntimeError("boom")
        return list(self._comments.get(post_id, [])[:limit])


class TestHeuristics:
    def test_cashtag_detected(self):
        assert has_ticker_mention("I am all in on $GME today") is True
        assert has_ticker_mention("no tickers here") is False

    def test_should_fetch_by_score(self):
        assert should_fetch_comments(make_post(score=100)) is True

    def test_should_fetch_by_comment_volume(self):
        assert should_fetch_comments(make_post(num_comments=50)) is True

    def test_should_fetch_by_ticker(self):
        assert should_fetch_comments(make_post(body="buy $AMC calls")) is True

    def test_low_signal_not_fetched(self):
        assert should_fetch_comments(make_post(score=1, num_comments=1)) is False


class TestIngest:
    def test_persists_posts_and_comments(self, repo):
        source = FakeRedditSource(
            posts=[make_post("p1", score=100)],
            comments={"p1": [make_comment("c1"), make_comment("c2")]},
        )
        result = ingest_once(repo, source, subreddit=SUB)
        assert result.posts_new == 1
        assert result.comments_new == 2
        assert result.posts_with_comments == 1
        post = repo.get_item("t3_p1")
        assert post is not None and post.kind.value == "post"
        comment = repo.get_item("t1_c1")
        assert comment is not None
        assert comment.kind.value == "comment"
        assert comment.parent_fullname == "t3_p1"

    def test_idempotent_rerun(self, repo):
        source = FakeRedditSource(
            posts=[make_post("p1", score=100)],
            comments={"p1": [make_comment("c1"), make_comment("c2")]},
        )
        ingest_once(repo, source, subreddit=SUB)
        second = ingest_once(repo, source, subreddit=SUB)
        assert second.posts_new == 0
        assert second.posts_duplicate == 1
        assert second.comments_new == 0
        assert second.comments_duplicate == 2
        assert repo.stats()["items_ingested"] == 3  # 1 post + 2 comments, unchanged

    def test_cursor_advances_to_newest(self, repo):
        # newest first: p2 (later created) then p1
        source = FakeRedditSource(
            posts=[
                make_post("p2", created=1_700_000_500.0),
                make_post("p1", created=1_700_000_000.0),
            ]
        )
        result = ingest_once(repo, source, subreddit=SUB)
        assert result.cursor_fullname == "t3_p2"
        cur = repo.get_cursor(f"{SUB}:new")
        assert cur is not None
        assert cur.last_fullname == "t3_p2"

    def test_selective_comment_fetching(self, repo):
        source = FakeRedditSource(
            posts=[
                make_post("hot", score=100),  # fetched (score)
                make_post("boring", score=0, num_comments=0),  # skipped
                make_post("ticker", score=0, body=("x" * 890) + " $TSLA"),  # fetched (ticker)
            ],
            comments={
                "hot": [make_comment("hc", parent="t3_hot")],
                "ticker": [make_comment("tc", parent="t3_ticker")],
            },
        )
        result = ingest_once(repo, source, subreddit=SUB)
        assert set(source.comment_calls) == {"hot", "ticker"}
        assert "boring" not in source.comment_calls
        assert result.posts_new == 3
        assert result.comments_new == 2
        assert result.posts_with_comments == 2

    def test_error_isolation(self, repo):
        source = FakeRedditSource(
            posts=[make_post("p1", score=100), make_post("p2", score=100)],
            comments={"p1": [make_comment("c1", parent="t3_p1")]},
            fail_comments_for=["p2"],
        )
        result = ingest_once(repo, source, subreddit=SUB)
        # both posts persisted despite p2's comment fetch raising
        assert result.posts_new == 2
        assert result.errors == 1
        assert repo.get_item("t3_p1") is not None
        assert repo.get_item("t3_p2") is not None
        assert repo.get_item("t1_c1") is not None

    def test_empty_listing_is_safe(self, repo):
        source = FakeRedditSource(posts=[])
        result = ingest_once(repo, source, subreddit=SUB)
        assert result.posts_new == 0
        assert result.cursor_fullname is None

    def test_invalid_created_epoch_falls_back_to_fetched_time(self, repo):
        source = FakeRedditSource(posts=[make_post("p_epoch0", created=0.0)])
        ingest_once(repo, source, subreddit=SUB)
        post = repo.get_item("t3_p_epoch0")
        assert post is not None
        assert post.created_utc == post.fetched_at

    def test_millisecond_created_epoch_is_normalized(self, repo):
        epoch_ms = 1_700_000_000_000
        source = FakeRedditSource(posts=[make_post("p_ms", created=epoch_ms)])
        ingest_once(repo, source, subreddit=SUB)
        post = repo.get_item("t3_p_ms")
        assert post is not None
        # 2023-11-14T22:13:20Z; ensures milliseconds were converted to seconds.
        assert int(post.created_utc.timestamp()) == 1_700_000_000

    def test_skips_posts_under_min_length(self, repo):
        source = FakeRedditSource(posts=[make_post("short", body="too short")])
        result = ingest_once(repo, source, subreddit=SUB)
        assert result.posts_new == 0
        assert repo.get_item("t3_short") is None

    def test_accepts_post_when_title_and_body_meet_min_length(self, repo):
        source = FakeRedditSource(
            posts=[make_post("tb_len", title="t" * 790, body="b" * 20)]
        )
        result = ingest_once(repo, source, subreddit=SUB)
        assert result.posts_new == 1
        assert repo.get_item("t3_tb_len") is not None

    def test_truncates_posts_to_max_length(self, repo):
        source = FakeRedditSource(posts=[make_post("long", body="a" * 1200)])
        ingest_once(repo, source, subreddit=SUB)
        post = repo.get_item("t3_long")
        assert post is not None
        assert len(post.body) == 800
