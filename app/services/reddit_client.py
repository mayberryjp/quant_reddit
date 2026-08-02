"""Reddit ingestion.

Reads new posts (and, selectively, their top-level comments) from a subreddit and
persists them idempotently into the ``reddit_items`` ledger, advancing a per-source
``ingest_cursor``.

Authentication uses an OAuth2 **script app** (password grant) via PRAW, as decided
for the platform. Reddit's Data API allows ~100 queries/minute per OAuth client id
(averaged over 10 minutes); to stay well under that we poll ``/new`` and only fetch
comments for high-signal posts (high score / comment volume / ticker mentions), per
the owner's guidance.

The ingestion pipeline is written against a small :class:`RedditSource` protocol so
tests can inject a fake source — no network is touched in tests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol

from app.config import settings
from app.models.domain import ProcessState, RedditItem, RedditKind
from app.repository.postgres import RedditRepository
from app.timeutil import utcnow

log = logging.getLogger("quant_reddit.reddit_client")

# Cashtag mention, e.g. "$GME" or "$aapl". Used only to decide whether a post is
# worth fetching comments for; real ticker extraction happens in the distiller.
_CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,5}\b")


# ----------------------------------------------------------------------------
# Normalized source objects (decoupled from PRAW)
# ----------------------------------------------------------------------------
@dataclass
class RawPost:
    fullname: str  # t3_...
    id: str
    title: str
    body: str
    author: str | None
    score: int
    permalink: str
    created_utc: float  # epoch seconds (UTC)
    num_comments: int = 0


@dataclass
class RawComment:
    fullname: str  # t1_...
    body: str
    author: str | None
    score: int
    permalink: str
    created_utc: float  # epoch seconds (UTC)
    parent_fullname: str | None = None


class RedditSource(Protocol):
    """Abstraction over the Reddit API used by the ingester."""

    def new_posts(self, subreddit: str, limit: int) -> Iterable[RawPost]: ...

    def post_comments(self, post_id: str, limit: int) -> Iterable[RawComment]: ...


# ----------------------------------------------------------------------------
# PRAW-backed source (production)
# ----------------------------------------------------------------------------
class PrawRedditSource:
    """A :class:`RedditSource` backed by PRAW using script-app (password) auth."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        user_agent: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        import praw  # imported lazily so tests never require live credentials

        self._reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            user_agent=user_agent,
            check_for_updates=False,
        )
        # We only read; never mutate Reddit state.
        self._reddit.read_only = not (username and password)

    @classmethod
    def from_settings(cls) -> "PrawRedditSource":
        return cls(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
            username=settings.reddit_username,
            password=settings.reddit_password,
        )

    def new_posts(self, subreddit: str, limit: int) -> Iterable[RawPost]:
        for s in self._reddit.subreddit(subreddit).new(limit=limit):
            yield RawPost(
                fullname=s.fullname,
                id=s.id,
                title=s.title or "",
                body=getattr(s, "selftext", "") or "",
                author=(str(s.author) if s.author else None),
                score=int(getattr(s, "score", 0) or 0),
                permalink=f"https://www.reddit.com{s.permalink}",
                created_utc=float(s.created_utc),
                num_comments=int(getattr(s, "num_comments", 0) or 0),
            )

    def post_comments(self, post_id: str, limit: int) -> Iterable[RawComment]:
        submission = self._reddit.submission(id=post_id)
        submission.comments.replace_more(limit=0)  # drop "load more" placeholders
        for c in submission.comments[:limit]:
            yield RawComment(
                fullname=c.fullname,
                body=getattr(c, "body", "") or "",
                author=(str(c.author) if c.author else None),
                score=int(getattr(c, "score", 0) or 0),
                permalink=f"https://www.reddit.com{c.permalink}",
                created_utc=float(c.created_utc),
                parent_fullname=getattr(c, "parent_id", None),
            )


# ----------------------------------------------------------------------------
# Ingestion pipeline
# ----------------------------------------------------------------------------
@dataclass
class IngestResult:
    posts_new: int = 0
    posts_duplicate: int = 0
    comments_new: int = 0
    comments_duplicate: int = 0
    posts_with_comments: int = 0
    errors: int = 0
    cursor_fullname: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _epoch_to_utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def has_ticker_mention(text: str) -> bool:
    return bool(_CASHTAG_RE.search(text or ""))


def should_fetch_comments(post: RawPost) -> bool:
    """Selective comment fetching to conserve Reddit API budget.

    Fetch a post's comments only when it looks high-signal: high score, high
    comment volume, or an explicit ticker (cashtag) mention.
    """
    if post.score >= settings.comment_min_score:
        return True
    if post.num_comments >= settings.comment_min_comments:
        return True
    return has_ticker_mention(f"{post.title}\n{post.body}")


def _post_to_item(post: RawPost, subreddit: str, fetched_at: datetime) -> RedditItem:
    return RedditItem(
        fullname=post.fullname,
        kind=RedditKind.post,
        subreddit=subreddit,
        author=post.author,
        title=post.title,
        body=post.body,
        score=post.score,
        permalink=post.permalink,
        parent_fullname=None,
        created_utc=_epoch_to_utc(post.created_utc),
        fetched_at=fetched_at,
        process_state=ProcessState.new,
    )


def _comment_to_item(
    comment: RawComment, subreddit: str, fetched_at: datetime
) -> RedditItem:
    return RedditItem(
        fullname=comment.fullname,
        kind=RedditKind.comment,
        subreddit=subreddit,
        author=comment.author,
        title=None,
        body=comment.body,
        score=comment.score,
        permalink=comment.permalink,
        parent_fullname=comment.parent_fullname,
        created_utc=_epoch_to_utc(comment.created_utc),
        fetched_at=fetched_at,
        process_state=ProcessState.new,
    )


def ingest_once(
    repo: RedditRepository,
    source: RedditSource,
    *,
    subreddit: str | None = None,
    post_batch: int | None = None,
    comments_per_post: int | None = None,
    cursor_key: str | None = None,
) -> IngestResult:
    """Run one ingestion cycle: fetch new posts + selective comments, persist
    idempotently, and advance the cursor.

    A failure ingesting one post/comment never aborts the batch — errors are
    counted and the remaining items proceed (graceful degradation).
    """
    subreddit = subreddit or settings.subreddits[0]
    post_batch = post_batch or settings.post_batch
    comments_per_post = comments_per_post or settings.comments_per_post
    cursor_key = cursor_key or f"{subreddit}:new"

    result = IngestResult()
    fetched_at = utcnow()

    try:
        posts = list(source.new_posts(subreddit, post_batch))
    except Exception:  # noqa: BLE001 - a listing failure ends the cycle cleanly
        log.exception("failed to fetch new posts for r/%s", subreddit)
        return result

    newest: RawPost | None = posts[0] if posts else None

    for post in posts:
        try:
            _, is_dup = repo.insert_item(_post_to_item(post, subreddit, fetched_at))
            if is_dup:
                result.posts_duplicate += 1
            else:
                result.posts_new += 1
        except Exception:  # noqa: BLE001 - isolate per-post failures
            log.exception("failed to persist post %s", post.fullname)
            result.errors += 1
            continue

        if not should_fetch_comments(post):
            continue
        result.posts_with_comments += 1
        try:
            comments = list(source.post_comments(post.id, comments_per_post))
        except Exception:  # noqa: BLE001 - isolate per-post comment failures
            log.exception("failed to fetch comments for post %s", post.fullname)
            result.errors += 1
            continue
        for comment in comments:
            try:
                _, is_dup = repo.insert_item(
                    _comment_to_item(comment, subreddit, fetched_at)
                )
                if is_dup:
                    result.comments_duplicate += 1
                else:
                    result.comments_new += 1
            except Exception:  # noqa: BLE001 - isolate per-comment failures
                log.exception("failed to persist comment %s", comment.fullname)
                result.errors += 1

    if newest is not None:
        repo.upsert_cursor(
            cursor_key,
            last_fullname=newest.fullname,
            last_created_utc=_epoch_to_utc(newest.created_utc),
        )
        result.cursor_fullname = newest.fullname

    return result
