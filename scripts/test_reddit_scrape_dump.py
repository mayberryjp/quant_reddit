"""Local smoke test using a real browser (Playwright + Chromium).

Fetches recent Reddit posts by rendering pages in Chromium, then dumps data to
local files. Optional full request/response transaction logging is built in.

Examples:
    python scripts/test_reddit_scrape_dump.py --subreddit wallstreetbets --limit 25
    python scripts/test_reddit_scrape_dump.py --subreddit stocks --limit 10 --with-comments
    python scripts/test_reddit_scrape_dump.py --transaction-log-file logs/reddit_txn.log
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Reddit posts with browser emulation and dump to local files."
    )
    parser.add_argument("--subreddit", default="wallstreetbets", help="Subreddit name.")
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Number of newest posts to fetch (default: 3).",
    )
    parser.add_argument(
        "--with-comments",
        action="store_true",
        help="Also fetch comments for each post into comments/<post_id>.json.",
    )
    parser.add_argument(
        "--comments-per-post",
        type=int,
        default=5,
        help="Maximum comments per post when --with-comments is set (default: 5).",
    )
    parser.add_argument(
        "--output-dir",
        default="local_dumps/reddit",
        help="Base output directory.",
    )
    parser.add_argument(
        "--profile-dir",
        default=".playwright-profile/reddit",
        help="Persistent Chromium profile directory (keeps cookies/session).",
    )
    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        help="Browser user agent.",
    )
    parser.add_argument(
        "--load-timeout-ms",
        type=int,
        default=60_000,
        help="Page navigation timeout in milliseconds.",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=3_000,
        help="Wait time after each navigation for dynamic content to render.",
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        default=2,
        help="Number of scrolls on listing page to load more posts.",
    )
    parser.add_argument(
        "--post-delay-seconds",
        type=int,
        default=60,
        help="Delay between post page fetches (default: 60 seconds).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium headed (useful for first-run manual login).",
    )
    parser.add_argument(
        "--no-print-transaction",
        dest="print_transaction",
        action="store_false",
        help="Disable printing full HTTP request/response transactions.",
    )
    parser.add_argument(
        "--transaction-log-file",
        default="",
        help="Optional path to also write transaction logs.",
    )
    parser.set_defaults(print_transaction=True)
    return parser.parse_args()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _normalize_url(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"https://www.reddit.com{href}"
    return href


def _extract_post_body(page) -> str:
    """Best-effort extraction of a Reddit post body/selftext from a post page."""
    candidates = [
        "[data-testid='post-content'] p",
        "[data-testid='post-content']",
        "shreddit-post [slot='text-body']",
        "shreddit-post",
        "article p",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if not loc.count():
                continue
            text = loc.inner_text(timeout=3_000).strip()
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue
    return ""


def _extract_post_title(page) -> str:
    candidates = [
        "h1",
        "[data-testid='post-title']",
        "shreddit-post [slot='title']",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if not loc.count():
                continue
            text = loc.inner_text(timeout=3_000).strip()
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue
    return ""


def main() -> int:
    args = _parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print("Playwright is required. Install with:")
        print("  pip install playwright")
        print("  playwright install chromium")
        print(f"Import error: {exc}")
        return 2

    run_dir = Path(args.output_dir) / f"{args.subreddit}_{_utc_stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    log_handle = None
    if args.transaction_log_file:
        log_path = Path(args.transaction_log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("w", encoding="utf-8")

    def emit(line: str) -> None:
        print(line)
        if log_handle is not None:
            log_handle.write(line + "\n")

    def on_request(request) -> None:
        if not args.print_transaction:
            return
        if "reddit.com" not in request.url:
            return
        emit("=" * 100)
        emit("REQUEST")
        emit(f"METHOD: {request.method}")
        emit(f"URL   : {request.url}")
        emit("HEADERS:")
        for k, v in request.headers.items():
            emit(f"  {k}: {v}")
        if request.post_data:
            emit("BODY:")
            emit(request.post_data)

    def on_response(response) -> None:
        if not args.print_transaction:
            return
        url = response.url
        if "reddit.com" not in url:
            return
        emit("-" * 100)
        emit("RESPONSE")
        emit(f"URL   : {url}")
        emit(f"STATUS: {response.status} {response.status_text}")
        emit("HEADERS:")
        headers = response.headers
        for k, v in headers.items():
            emit(f"  {k}: {v}")
        body_text = ""
        try:
            body_text = response.text()
        except Exception as exc:  # noqa: BLE001
            emit(f"BODY READ FAILED: {exc}")
        if body_text:
            emit("BODY:")
            emit(body_text)
            emit(f"BODY LENGTH: {len(body_text)}")

    posts_payload: list[dict] = []
    comments_payload: dict[str, list[dict]] = {}
    listing_url = f"https://www.reddit.com/r/{args.subreddit}/new/"

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(Path(args.profile_dir).resolve()),
                headless=not args.headed,
                viewport={"width": 1440, "height": 1800},
                user_agent=args.user_agent,
                locale="en-US",
                timezone_id="UTC",
            )
            context.on("request", on_request)
            context.on("response", on_response)

            page = context.new_page()
            page.goto(listing_url, wait_until="domcontentloaded", timeout=args.load_timeout_ms)
            page.wait_for_timeout(args.settle_ms)

            for _ in range(max(0, args.scrolls)):
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
                    seen.add(href);
                    const text = (a.textContent || '').trim();
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

            for row in rows[: max(1, args.limit)]:
                permalink = _normalize_url(row.get("permalink") or "")
                post_id = row.get("id") or ""
                posts_payload.append(
                    {
                        "id": post_id,
                        "title": row.get("title") or "",
                        "permalink": permalink,
                        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                    }
                )

            for index, post in enumerate(posts_payload):
                pid = str(post.get("id") or "").strip() or "unknown"
                purl = str(post.get("permalink") or "").strip()
                if not purl:
                    post["body"] = ""
                    if args.with_comments:
                        comments_payload[pid] = []
                    continue

                page.goto(purl, wait_until="domcontentloaded", timeout=args.load_timeout_ms)
                page.wait_for_timeout(args.settle_ms)

                post["title"] = _extract_post_title(page) or post.get("title") or ""
                post["body"] = _extract_post_body(page)

                if args.with_comments:
                    extracted = page.eval_on_selector_all(
                        "[data-testid='comment'], shreddit-comment, div.Comment",
                        r"""
                        (els) => {
                          const out = [];
                          for (const el of els) {
                            const bodyEl = el.querySelector('[data-testid="comment"] p, p, div[slot="comment"]');
                            const text = (bodyEl?.textContent || el.textContent || '').trim();
                            if (!text) continue;
                            out.push({ body: text });
                          }
                          return out;
                        }
                        """,
                    )

                    comments_payload[pid] = extracted[: max(1, args.comments_per_post)]

                if index + 1 < len(posts_payload):
                    page.wait_for_timeout(max(0, args.post_delay_seconds) * 1000)

            context.close()
    except Exception as exc:  # noqa: BLE001
        emit(f"Browser scraping failed: {exc}")
        if log_handle is not None:
            log_handle.close()
        return 1

    _write_json(run_dir / "posts.json", posts_payload)
    with (run_dir / "posts.jsonl").open("w", encoding="utf-8") as f:
        for p in posts_payload:
            f.write(json.dumps(p, ensure_ascii=True) + "\n")

    normalized_posts: list[dict] = []
    for post in posts_payload:
        pid = str(post.get("id") or "").strip() or "unknown"
        normalized_posts.append(
            {
                "id": pid,
                "title": post.get("title") or "",
                "body": post.get("body") or "",
                "permalink": post.get("permalink") or "",
                "fetched_at": post.get("fetched_at_utc") or "",
                "comments": comments_payload.get(pid, []),
            }
        )

    _write_json(run_dir / "posts_normalized.json", normalized_posts)
    with (run_dir / "posts_normalized.jsonl").open("w", encoding="utf-8") as f:
        for post in normalized_posts:
            f.write(json.dumps(post, ensure_ascii=True) + "\n")

    summary: dict[str, object] = {
        "subreddit": args.subreddit,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "post_count": len(posts_payload),
        "with_comments": bool(args.with_comments),
        "comments_per_post": args.comments_per_post,
        "post_delay_seconds": args.post_delay_seconds,
        "listing_url": listing_url,
        "files": ["posts.json", "posts.jsonl", "posts_normalized.json", "posts_normalized.jsonl"],
    }

    if args.with_comments:
        comments_dir = run_dir / "comments"
        total_comments = 0
        for post_id, comments in comments_payload.items():
            _write_json(comments_dir / f"{post_id}.json", comments)
            total_comments += len(comments)
        summary["comment_file_count"] = len(comments_payload)
        summary["comment_count"] = total_comments
        summary["files"].append("comments/*.json")

    _write_json(run_dir / "summary.json", summary)

    emit(f"Wrote Reddit dump to: {run_dir}")
    emit(f"Posts: {len(posts_payload)}")
    if args.with_comments:
        emit(f"Comment files: {summary.get('comment_file_count', 0)}")
        emit(f"Comments: {summary.get('comment_count', 0)}")

    if log_handle is not None:
        log_handle.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
