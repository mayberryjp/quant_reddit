"""Reddit ingestion.

Reads new posts (and, selectively, their top-level comments) from a subreddit and
persists them idempotently into the ``reddit_items`` ledger, advancing a per-source
``ingest_cursor``.

Authentication can use either OAuth2 (via PRAW) or browser-backed scraping via
Playwright, depending on ``REDDIT_SOURCE_MODE`` and available credentials. To
stay well under practical rate limits we poll ``/new`` and only fetch comments
for high-signal posts (high score / comment volume / ticker mentions), per the
owner's guidance.

The ingestion pipeline is written against a small :class:`RedditSource` protocol so
tests can inject a fake source — no network is touched in tests.
"""

from __future__ import annotations

import logging
import html as html_module
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
# Sources (production)
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


class PlaywrightRedditSource:
    """A :class:`RedditSource` backed by browser rendering with Playwright.

    This path emulates a browser and extracts the same page data used by the
    working local test script, avoiding the blocked reddit.com JSON endpoints.
    """

    def __init__(
        self,
        *,
        profile_dir: str,
        user_agent: str,
        load_timeout_ms: int,
        settle_ms: int,
        scrolls: int,
        post_delay_seconds: int,
        comments_per_post: int,
    ) -> None:
        self._profile_dir = str(Path(profile_dir).resolve())
        self._user_agent = user_agent
        self._load_timeout_ms = load_timeout_ms
        self._settle_ms = settle_ms
        self._scrolls = scrolls
        self._post_delay_seconds = post_delay_seconds
        self._comments_per_post = comments_per_post

    @classmethod
    def from_settings(cls) -> "PlaywrightRedditSource":
        return cls(
            user_agent=settings.reddit_user_agent,
            profile_dir=getattr(settings, "reddit_profile_dir", ".playwright-profile/reddit"),
            load_timeout_ms=int(settings.http_timeout * 1000),
            settle_ms=3000,
            scrolls=2,
            post_delay_seconds=max(0, int(getattr(settings, "reddit_post_delay_seconds", 60))),
            comments_per_post=max(1, int(settings.comments_per_post)),
        )

    def _fetch_page_dump(self, subreddit: str, limit: int) -> list[dict]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Playwright is required for browser-backed Reddit scraping"
            ) from exc

        listing_url = f"https://www.reddit.com/r/{subreddit}/new/"
        posts_payload: list[dict] = []

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=self._profile_dir,
                headless=True,
                viewport={"width": 1440, "height": 1800},
                user_agent=self._user_agent,
                locale="en-US",
                timezone_id="UTC",
            )
            page = context.new_page()
            page.goto(listing_url, wait_until="domcontentloaded", timeout=self._load_timeout_ms)
            page.wait_for_timeout(self._settle_ms)

            for _ in range(max(0, self._scrolls)):
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(1200)

            rows = page.eval_on_selector_all(
                "a[href*='/comments/']",
                r"""
                (els) => {
                  const out = [];
                  const seen = new Set();
                  for (const a of els) {
                    const href = a.getAttribute('href') || '';
                    if (!href.includes('/comments/')) continue;
                    if (seen.has(href)) continue;
                    // Card-wrapping anchors carry the whole post (title + body +
                    // flair); only the post-title anchor holds the title alone.
                    const isTitleAnchor =
                      (a.id || '').startsWith('post-title-') ||
                      a.getAttribute('slot') === 'title' ||
                      a.getAttribute('data-click-id') === 'body';
                    if (!isTitleAnchor) continue;
                    seen.add(href);
                    const text = (a.getAttribute('aria-label') || a.textContent || '').trim();
                    const m = href.match(/\/comments\/([a-z0-9]+)\//i);
                    out.push({
                      id: m ? m[1] : null,
                      title: text,
                      permalink: href,
                    });
                  }
                  return out;
                }
                """,
            )

            for row in rows[: max(1, limit)]:
                permalink = str(row.get("permalink") or "")
                if permalink.startswith("/"):
                    permalink = f"https://www.reddit.com{permalink}"
                posts_payload.append(
                    {
                        "id": str(row.get("id") or "").strip(),
                        "title": str(row.get("title") or ""),
                        "permalink": permalink,
                    }
                )

            context.close()

        return posts_payload

    def new_posts(self, subreddit: str, limit: int) -> Iterable[RawPost]:
        for post in self._fetch_page_dump(subreddit, limit):
            permalink = str(post.get("permalink") or "")
            post_id = str(post.get("id") or "").strip()
            page_html = self._fetch_post_html(permalink)
            title, body, author, score, created_utc, num_comments = self._parse_post_html(
                page_html, fallback_title=str(post.get("title") or ""), fallback_id=post_id
            )
            yield RawPost(
                fullname=f"t3_{post_id}" if post_id else "",
                id=post_id,
                title=title,
                body=body,
                author=author,
                score=score,
                permalink=permalink,
                created_utc=created_utc,
                num_comments=num_comments,
            )

    def post_comments(self, post_id: str, limit: int) -> Iterable[RawComment]:
        comments = self._fetch_post_comments(post_id, limit)
        for comment in comments:
            yield comment

    def _fetch_post_html(self, permalink: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Playwright is required for browser-backed Reddit scraping"
            ) from exc

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=self._profile_dir,
                headless=True,
                viewport={"width": 1440, "height": 1800},
                user_agent=self._user_agent,
                locale="en-US",
                timezone_id="UTC",
            )
            page = context.new_page()
            page.goto(permalink, wait_until="domcontentloaded", timeout=self._load_timeout_ms)
            page.wait_for_timeout(self._settle_ms)
            html = page.content()
            context.close()
            return html

    def _parse_post_html(
        self, page_html: str, *, fallback_title: str, fallback_id: str
    ) -> tuple[str, str, str | None, int, float, int]:
        title = fallback_title
        body = ""
        author: str | None = None
        score = 0
        created_utc = 0.0
        num_comments = 0

        # Try to extract from embedded reddit-page-data JSON first (most reliable)
        match = re.search(r'<reddit-page-data data="([^"]+)"', page_html)
        if match:
            try:
                data = json.loads(html_module.unescape(match.group(1)))
                if isinstance(data, dict):
                    # Try to get post data from the root
                    post_data = data.get("post")
                    if isinstance(post_data, dict):
                        title = str(post_data.get("title") or "") or fallback_title
                        body = str(post_data.get("selftext", post_data.get("body") or "")) or ""
                    
                    # Extract author from subreddit info
                    subreddit_data = data.get("subreddit")
                    if isinstance(subreddit_data, dict):
                        author = str(subreddit_data.get("name") or None) or None
                    
                    # Extract score, comment count, and timestamps
                    if isinstance(post_data, dict):
                        if "score" in post_data:
                            score = int(post_data.get("score") or 0)
                        if "number_comments" in post_data:
                            num_comments = int(post_data.get("number_comments") or 0)
                        if "created_timestamp" in post_data:
                            created_utc = float(post_data.get("created_timestamp") or 0)
            except Exception:  # noqa: BLE001
                pass

        # Fallback: extract from HTML if JSON parsing didn't work
        if not title or title == fallback_title:
            title_match = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", page_html)
            if title_match:
                stripped = re.sub(r"<[^>]+>", " ", title_match.group(1))
                stripped = html_module.unescape(stripped)
                stripped = re.sub(r"\s+", " ", stripped).strip()
                if stripped:
                    title = stripped
        
        if not body:
            # Look for post body content in various HTML structures
            text_match = re.search(r'<article[\s\S]*?<p[^>]*>(.*?)</p>', page_html)
            if text_match:
                body = re.sub(r"<[^>]+>", "", text_match.group(1)).strip()
            else:
                # Try div-based body extraction
                div_match = re.search(r'<div[^>]*class="[^"]*post-content[^"]*"[^>]*>(.*?)</div>', page_html)
                if div_match:
                    body = re.sub(r"<[^>]+>", "", div_match.group(1)).strip()

        # Extract metadata if not already found
        if score == 0:
            score_match = re.search(r'"score"\s*:\s*(\d+)', page_html)
            if score_match:
                score = int(score_match.group(1))

        if num_comments == 0:
            comments_match = re.search(r'"number_comments"\s*:\s*(\d+)', page_html)
            if comments_match:
                num_comments = int(comments_match.group(1))

        if created_utc == 0.0:
            created_match = re.search(r'"created_timestamp"\s*:\s*(\d+)', page_html)
            if created_match:
                created_utc = float(created_match.group(1))

        return title, body, author, score, created_utc, num_comments

    def _fetch_post_comments(self, post_id: str, limit: int) -> list[RawComment]:
        # Best-effort comment extraction from rendered page HTML. This is intended
        # to track the same data the local test script retrieves.
        url = f"https://www.reddit.com/comments/{post_id}/"
        html = self._fetch_post_html(url)
        comments: list[RawComment] = []
        seen: set[str] = set()
        for match in re.finditer(r'<shreddit-comment[^>]*>([\s\S]*?)</shreddit-comment>', html):
            snippet = match.group(1)
            text = re.sub(r"<[^>]+>", " ", snippet)
            text = re.sub(r"\s+", " ", text).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            comments.append(
                RawComment(
                    fullname=f"t1_{len(comments) + 1}",
                    body=text,
                    author=None,
                    score=0,
                    permalink=url,
                    created_utc=0.0,
                    parent_fullname=f"t3_{post_id}",
                )
            )
            if len(comments) >= max(1, limit):
                break
        return comments


def build_reddit_source() -> RedditSource:
    """Build the Reddit source from settings.

    Modes:
    - ``praw``: always use OAuth/PRAW (requires credentials)
    - ``scrape``: always use public reddit.com JSON endpoints
    - ``auto``: use PRAW if credentials are present, otherwise scrape
    """

    mode = (settings.reddit_source_mode or "auto").strip().lower()
    has_oauth = bool(settings.reddit_client_id and settings.reddit_client_secret)

    if mode == "praw":
        if not has_oauth:
            raise ValueError(
                "REDDIT_SOURCE_MODE=praw requires REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET"
            )
        return PrawRedditSource.from_settings()

    if mode == "scrape":
        return PlaywrightRedditSource.from_settings()

    if mode != "auto":
        raise ValueError("REDDIT_SOURCE_MODE must be one of: auto, praw, scrape")

    if has_oauth:
        log.info("reddit source mode auto: using PRAW OAuth source")
        return PrawRedditSource.from_settings()

    log.warning(
        "reddit source mode auto: REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET missing; "
        "falling back to browser-backed scraping"
    )
    return PlaywrightRedditSource.from_settings()


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


def _normalize_epoch(epoch: float | int | None) -> float | None:
    """Normalize epoch values from source payloads.

    Accepts either seconds or milliseconds. Returns ``None`` when the value is
    missing or invalid.
    """
    if epoch is None:
        return None
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Millisecond epoch values are common in web payloads.
    if value > 10_000_000_000:
        value /= 1000.0
    return value


def _safe_created_utc(epoch: float | int | None, fallback: datetime) -> datetime:
    normalized = _normalize_epoch(epoch)
    if normalized is None:
        return fallback
    return _epoch_to_utc(normalized)


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


def _post_text_length(title: str, body: str) -> int:
    return len((title or "") + (body or ""))


def _should_ingest_post(title: str, body: str) -> bool:
    return _post_text_length(title, body) >= settings.post_min_chars


def _truncate_post_body(body: str) -> str:
    if settings.post_max_chars > 0 and len(body) > settings.post_max_chars:
        return body[: settings.post_max_chars]
    return body


def _post_to_item(post: RawPost, subreddit: str, fetched_at: datetime) -> RedditItem:
    body = _truncate_post_body(post.body or "")
    return RedditItem(
        fullname=post.fullname,
        kind=RedditKind.post,
        subreddit=subreddit,
        author=post.author,
        title=post.title,
        body=body,
        score=post.score,
        permalink=post.permalink,
        parent_fullname=None,
        created_utc=_safe_created_utc(post.created_utc, fetched_at),
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
        created_utc=_safe_created_utc(comment.created_utc, fetched_at),
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
        if not _should_ingest_post(post.title or "", post.body or ""):
            continue
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
            last_created_utc=_safe_created_utc(newest.created_utc, fetched_at),
        )
        result.cursor_fullname = newest.fullname

    return result
