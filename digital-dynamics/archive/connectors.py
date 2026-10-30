"""
connectors.py
=============
Two ways to get real data into the analyzer *without* scraping anyone:

A. Live public APIs — Mastodon (no credentials at all) and Reddit (free OAuth
   app credentials, client-credentials grant, public data only).
B. Import of a data-subject's own platform export — see `importers.py`.

Both paths end in the exact same post schema as `osint_core`, so every analysis,
finding and report works unchanged.

Network functions are kept separate from pure mapping functions
(`mastodon_to_posts`, `reddit_to_posts`) so the mapping logic is unit-testable
offline from fixtures.
"""

from __future__ import annotations

import base64
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = "digital-dynamics-osint/0.2 (consent-based research tool)"
DEFAULT_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# Shared plumbing
# --------------------------------------------------------------------------- #
@dataclass
class FetchResult:
    posts: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.posts) and not self.errors


def _get_json(url: str, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _human_error(exc: Exception, what: str) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code == 401:
            return f"{what}: 401 Unauthorized — credentials rejected."
        if code == 403:
            return f"{what}: 403 Forbidden — the instance/API refused this request (rate limit or blocked)."
        if code == 404:
            return f"{what}: 404 Not Found — check the handle / instance spelling."
        if code == 429:
            return f"{what}: 429 Too Many Requests — you are being rate limited. Wait a minute."
        return f"{what}: HTTP {code}."
    if isinstance(exc, urllib.error.URLError):
        return f"{what}: network unreachable ({exc.reason})."
    if isinstance(exc, (TimeoutError, OSError)):
        return f"{what}: network unreachable ({exc})."
    return f"{what}: {type(exc).__name__} — {exc}"


def strip_html(text: str) -> str:
    """Mastodon/Reddit bodies are HTML. Keep the words, drop the markup."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>\s*<p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _iso(value) -> str | None:
    """ISO 8601 -> naive local-ish ISO string the schema uses."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None).isoformat()
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return text
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# A1 · Mastodon — public API, zero credentials
# --------------------------------------------------------------------------- #
def mastodon_to_posts(account: dict, statuses: list[dict], contexts: dict[str, dict] | None = None) -> FetchResult:
    """Pure mapping: Mastodon account + statuses (+ per-status context) -> posts."""
    contexts = contexts or {}
    handle = "@" + (account.get("acct") or account.get("username") or "unknown")
    res = FetchResult(
        meta={
            "platform": "mastodon",
            "account": handle,
            "display_name": account.get("display_name") or "",
            "statuses_on_account": account.get("statuses_count"),
            "statuses_fetched": len(statuses),
            "threads_fetched": len(contexts),
            "scope": "public statuses only (instance public API)",
        }
    )

    for status in statuses:
        body = status.get("reblog") or status
        text = strip_html(body.get("content") or "")
        if not text:
            continue

        comments = []
        ctx = contexts.get(str(status.get("id")))
        for reply in (ctx or {}).get("descendants", []) or []:
            reply_text = strip_html(reply.get("content") or "")
            if not reply_text:
                continue
            comments.append(
                {
                    "user": "@" + (reply.get("account", {}).get("acct") or "unknown"),
                    "text": reply_text,
                    "timestamp": _iso(reply.get("created_at")),
                }
            )

        tags = ["@" + m.get("acct", "") for m in (body.get("mentions") or []) if m.get("acct")]

        res.posts.append(
            {
                "id": f"mastodon_{status.get('id')}",
                "author": handle,
                "post_text": text,
                "timestamp": _iso(status.get("created_at")),
                "location": None,  # Mastodon has no venue field; co-location is skipped
                "comments": comments,
                "tags": tags,
                "checkins": [],
                "url": status.get("url"),
                "boosted": bool(status.get("reblog")),
            }
        )

    if not res.posts:
        res.errors.append("No public statuses with text were returned for this account.")
    elif not any(p["comments"] for p in res.posts):
        res.warnings.append(
            "No replies found in the fetched threads — network analysis will be empty. "
            "Only public replies are visible; most conversation happens in mentions and DMs."
        )
    res.warnings.append(
        "Mastodon has no venue/check-in field, so location overlap is not analysable from this source."
    )
    return res


def mastodon_fetch(
    instance: str,
    handle: str,
    limit: int = 40,
    include_replies: bool = True,
    max_threads: int = 20,
    delay: float = 0.4,
    progress=None,
) -> FetchResult:
    """Fetch an account's public statuses + reply threads from a Mastodon instance."""
    base = "https://" + instance.strip().strip("/").replace("https://", "").replace("http://", "")
    acct = handle.strip().lstrip("@")
    if "@" in acct:  # user typed user@instance
        acct = acct.split("@")[0]

    try:
        account = _get_json(f"{base}/api/v1/accounts/lookup?acct={urllib.parse.quote(acct)}")
    except Exception as exc:
        return FetchResult(errors=[_human_error(exc, f"Looking up @{acct} on {instance}")])

    if progress:
        progress(0.15, f"Found @{account.get('acct')} — fetching statuses…")

    statuses: list[dict] = []
    max_id = None
    while len(statuses) < limit:
        page = min(40, limit - len(statuses))
        url = (
            f"{base}/api/v1/accounts/{account['id']}/statuses?limit={page}"
            f"&exclude_replies={'false' if include_replies else 'true'}&exclude_reblogs=false"
        )
        if max_id:
            url += f"&max_id={max_id}"
        try:
            batch = _get_json(url)
        except Exception as exc:
            return FetchResult(errors=[_human_error(exc, "Fetching statuses")], meta={"account": acct})
        if not batch:
            break
        statuses.extend(batch)
        max_id = batch[-1]["id"]
        if len(batch) < page:
            break
        time.sleep(delay)

    # Thread replies (bounded and polite — public endpoints only)
    contexts: dict[str, dict] = {}
    for i, status in enumerate(statuses[:max_threads]):
        try:
            contexts[str(status["id"])] = _get_json(f"{base}/api/v1/statuses/{status['id']}/context")
        except Exception:
            pass  # a single unreachable thread must not kill the run
        if progress:
            progress(0.15 + 0.6 * (i + 1) / max(min(len(statuses), max_threads), 1), f"Reading thread {i + 1}/{min(len(statuses), max_threads)}…")
        time.sleep(delay)

    return mastodon_to_posts(account, statuses, contexts)


# --------------------------------------------------------------------------- #
# A2 · Reddit — free OAuth app credentials (client-credentials grant)
# --------------------------------------------------------------------------- #
def reddit_to_posts(username: str, submissions: list[dict], comments_by_post: dict[str, list[dict]] | None = None) -> FetchResult:
    """Pure mapping: Reddit submission listings (+ per-post comment listings) -> posts."""
    comments_by_post = comments_by_post or {}
    handle = "@" + username.lstrip("u/").lstrip("@")
    res = FetchResult(
        meta={
            "platform": "reddit",
            "account": handle,
            "submissions_fetched": len(submissions),
            "threads_fetched": len(comments_by_post),
            "scope": "public submissions and their public comment threads",
        }
    )

    for sub in submissions:
        data = sub.get("data", sub)
        title = (data.get("title") or "").strip()
        selftext = (data.get("selftext") or "").strip()
        text = (title + ("\n\n" + selftext if selftext else "")).strip()
        if not text or data.get("author", username).lower() != username.lstrip("u/").lstrip("@").lower():
            continue

        comments = []
        for c in comments_by_post.get(str(data.get("id")), []):
            cd = c.get("data", c)
            body = (cd.get("body") or "").strip()
            author = cd.get("author") or "[deleted]"
            if not body or author in ("[deleted]", "AutoModerator"):
                continue
            comments.append(
                {
                    "user": "@" + author,
                    "text": body,
                    "timestamp": _iso(cd.get("created_utc")),
                }
            )

        res.posts.append(
            {
                "id": f"reddit_{data.get('id')}",
                "author": handle,
                "post_text": text,
                "timestamp": _iso(data.get("created_utc")),
                "location": None,  # subreddit is not a location — do not fake one
                "comments": comments,
                "tags": [],
                "checkins": [],
                "subreddit": data.get("subreddit"),
                "url": "https://reddit.com" + data.get("permalink", ""),
            }
        )

    if not res.posts:
        res.errors.append(
            "No public submissions found for that account (it may only comment, or be suspended/private)."
        )
    res.warnings.append(
        "Reddit exposes no location data, so location overlap is not analysable from this source. "
        "Subreddit names are kept separately and never treated as a venue."
    )
    return res


def reddit_fetch(
    username: str,
    client_id: str,
    client_secret: str,
    limit: int = 50,
    max_threads: int = 10,
    delay: float = 0.7,
    progress=None,
) -> FetchResult:
    """Fetch public submissions + comment threads. Needs a free Reddit app
    (https://www.reddit.com/prefs/apps → 'script' type) — client-credentials grant."""
    user = username.strip().lstrip("u/").lstrip("@")
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        token_resp = _get_json(
            "https://www.reddit.com/api/v1/access_token",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            data=b"grant_type=client_credentials",
        )
        token = token_resp["access_token"]
    except Exception as exc:
        return FetchResult(errors=[_human_error(exc, "Reddit authentication")])

    headers = {"Authorization": f"bearer {token}"}
    try:
        listing = _get_json(
            f"https://oauth.reddit.com/user/{urllib.parse.quote(user)}/submitted?limit={min(limit, 100)}&raw_json=1",
            headers=headers,
        )
        submissions = listing["data"]["children"]
    except Exception as exc:
        return FetchResult(errors=[_human_error(exc, f"Fetching u/{user} submissions")])

    comments_by_post: dict[str, list[dict]] = {}
    for i, sub in enumerate(submissions[:max_threads]):
        sid = str(sub.get("data", sub).get("id"))
        try:
            thread = _get_json(
                f"https://oauth.reddit.com/comments/{sid}?limit=100&raw_json=1", headers=headers
            )
            if isinstance(thread, list) and len(thread) > 1:
                comments_by_post[sid] = thread[1]["data"]["children"]
        except Exception:
            pass
        if progress:
            progress(0.2 + 0.7 * (i + 1) / max(min(len(submissions), max_threads), 1), f"Reading thread {i + 1}…")
        time.sleep(delay)

    return reddit_to_posts(user, submissions, comments_by_post)
