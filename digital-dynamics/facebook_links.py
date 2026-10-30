"""
facebook_links.py
=================
Parses a **pasted Facebook link** — profile, Page, post, photo, group, share link —
into something the app can use, and (just as importantly) says clearly what a link
can and cannot do.

What a pasted link is used for here:
  · as the **identity label** for the analysis ("this export is about @handle");
  · as a **cross-check** against the owner detected inside the export, because
    people regularly hand over a friend's archive by mistake;
  · as a **Page identifier** for the Graph API route (Pages you administer only).

What it is not: a fetch instruction. Facebook does not allow reading personal
profiles programmatically, so pasting a profile link never triggers a scrape.
The app maps exports; links identify and cross-check.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "mbasic.facebook.com",
    "d.facebook.com",
    "fb.com",
    "www.fb.com",
    "fb.me",
    "l.facebook.com",
    "lm.facebook.com",
    "m.me",
}

# path segments that are functions, never usernames
RESERVED = {
    "profile.php", "people", "pages", "groups", "watch", "photo", "photo.php", "photos",
    "permalink.php", "story.php", "share", "reel", "reels", "videos", "events", "marketplace",
    "media", "p", "permalink", "login", "home.php", "friends", "help", "business", "ads",
    "policy.php", "privacy", "settings", "notifications", "messages", "gaming", "fundraisers",
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "mibextid",
    "rdid", "sfnsn", "ref", "refid", "fref", "hc_ref", "hc_location", "__cft__", "__tn__",
    "_rdr", "paipv", "eav", "comment_id", "reply_comment_id", "notif_id", "notif_t",
    "acontext", "checkpoint_src", "s", "t", "wstartindex", "wstartindex",
}

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{1,79}$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean_query(query: str) -> tuple[dict[str, list[str]], list[str]]:
    """Drop tracking noise; keep only params that carry an identifier."""
    parsed = parse_qs(query or "")
    dropped = [k for k in parsed if k.lower() in TRACKING_PARAMS or k.lower().startswith("utm_")]
    kept = {k: v for k, v in parsed.items() if k not in dropped}
    return kept, dropped


def _canonical(path: str, params: dict[str, str | None] | None = None) -> str:
    """Canonical facebook.com URL from a path plus the params that carry identity."""
    path = "/" + path.strip("/") if path.strip("/") else ""
    kept = {k: v for k, v in (params or {}).items() if v}
    query_str = ("?" + "&".join(f"{k}={v}" for k, v in kept.items())) if kept else ""
    return f"https://www.facebook.com{path}{query_str}"


def _result(raw: str, kind: str, **kw) -> dict:
    out = {
        "raw": raw,
        "kind": kind,
        "username": None,
        "numeric_id": None,
        "post_id": None,
        "canonical": None,
        "resolvable": False,
        "identifier": None,
        "notes": [],
    }
    out.update(kw)
    if out["identifier"] is None:
        out["identifier"] = out["username"] or out["numeric_id"]
    return out


def looks_like_url(text: str) -> bool:
    return bool(URL_RE.search(text or "")) or "facebook.com" in (text or "").lower()


# --------------------------------------------------------------------------- #
# Main parser
# --------------------------------------------------------------------------- #
def parse_facebook_url(text: str) -> dict | None:
    """Parse a pasted link, or a bare username. Returns a dict (never raises)."""
    raw = (text or "").strip()
    if not raw:
        return None

    match = URL_RE.search(raw)
    if not match and not looks_like_url(raw):
        # bare handle, e.g. "zuck" or "@nadia.rahman"
        handle = raw.lstrip("@").strip().strip("/")
        if HANDLE_RE.match(handle):
            return _result(
                raw,
                "profile",
                username=handle,
                canonical=f"https://www.facebook.com/{handle}",
                resolvable=True,
                notes=["No URL detected — read as a profile username."],
            )
        return _result(raw, "invalid", notes=["That does not look like a Facebook link or username."])

    url_text = match.group(0) if match else raw
    if not url_text.lower().startswith(("http://", "https://")):
        url_text = "https://" + url_text.lstrip("/")

    parsed = urlparse(url_text)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in FB_HOSTS and not host.endswith(".facebook.com"):
        return _result(
            raw,
            "unknown",
            canonical=parsed._replace(query="", fragment="").geturl(),
            notes=[f"{host or 'That host'} is not a Facebook domain — this app only resolves Facebook links."],
        )

    query, dropped = _clean_query(parsed.query)
    segments = [s for s in parsed.path.split("/") if s]
    notes: list[str] = []
    if dropped:
        notes.append(f"Ignored {len(dropped)} tracking parameter(s) ({', '.join(sorted(set(dropped))[:4])}).")

    # ---- short links ------------------------------------------------------
    if host in {"fb.me", "m.me"} or (segments and segments[0] in {"share", "l"}):
        return _result(
            raw,
            "short_link",
            canonical=f"https://{host}{parsed.path}",
            notes=notes + [
                "Shortened / share links hide the real target. Open the link in a browser, copy the full "
                "facebook.com/... address from the address bar, and paste that instead."
            ],
        )

    # ---- profile.php?id= --------------------------------------------------
    if segments and segments[0] == "profile.php":
        pid = (query.get("id") or [None])[0]
        if pid:
            return _result(raw, "profile", numeric_id=pid,
                           canonical=f"https://www.facebook.com/profile.php?id={pid}", resolvable=True, notes=notes)
        return _result(raw, "profile", canonical=_canonical(parsed.path, {k: v[0] for k, v in query.items()}),
                       notes=notes + ["No `id` in the link — incomplete profile URL."])

    # ---- permalink.php / story.php ---------------------------------------
    if segments and segments[0] in {"permalink.php", "story.php"}:
        story = (query.get("story_fbid") or query.get("fbid") or [None])[0]
        owner = (query.get("id") or [None])[0]
        return _result(
            raw, "post", post_id=story, numeric_id=owner,
            canonical=_canonical(segments[0], {"story_fbid": story, "id": owner}), resolvable=bool(story),
            notes=notes + ["This is a single post, not a profile — use it to point at one item, not to label an account."],
        )

    # ---- pages/Name/123456 ----------------------------------------------
    if segments and segments[0] == "pages":
        name = segments[1] if len(segments) > 1 else None
        pid = segments[2] if len(segments) > 2 else (query.get("id") or [None])[0]
        return _result(raw, "page", username=name, numeric_id=pid,
                       canonical=f"https://www.facebook.com/{pid}" if pid else _canonical(parsed.path, {k: v[0] for k, v in query.items()}),
                       resolvable=bool(pid or name),
                       notes=notes + ["Page link — fetchable through the Graph API only if you administer the Page."])

    # ---- groups/123/posts/456 -------------------------------------------
    if segments and segments[0] == "groups":
        gid = segments[1] if len(segments) > 1 else None
        post_id = segments[3] if len(segments) > 3 and segments[2] == "posts" else None
        return _result(raw, "group_post" if post_id else "group", numeric_id=gid, post_id=post_id,
                       canonical=_canonical(parsed.path, {k: v[0] for k, v in query.items()}), resolvable=bool(gid),
                       notes=notes + ["Group content is not in a personal export — the app cannot analyse it."])

    # ---- photos / videos / reels / watch --------------------------------
    if segments and segments[0] in {"photo", "photo.php", "photos", "watch", "reel", "reels", "videos"}:
        fid = (query.get("fbid") or query.get("v") or [None])[0] or (segments[1] if len(segments) > 1 else None)
        if query.get("v"):
            canon = _canonical("watch", {"v": query["v"][0]})
        elif query.get("fbid"):
            canon = _canonical("photo", {"fbid": query["fbid"][0]})
        else:
            canon = _canonical(parsed.path, {})
        return _result(raw, "post", post_id=fid, canonical=canon, resolvable=bool(fid),
                       notes=notes + ["Single media item, not a profile."])

    # ---- people/Name/123 ------------------------------------------------
    if segments and segments[0] == "people":
        pid = segments[2] if len(segments) > 2 else None
        return _result(raw, "profile", numeric_id=pid,
                       canonical=f"https://www.facebook.com/profile.php?id={pid}" if pid else _canonical(parsed.path, {k: v[0] for k, v in query.items()}),
                       resolvable=bool(pid), notes=notes)

    if not segments:
        pid = (query.get("id") or [None])[0]
        return _result(raw, "profile" if pid else "unknown", numeric_id=pid,
                       canonical=f"https://www.facebook.com/profile.php?id={pid}" if pid else None,
                       resolvable=bool(pid),
                       notes=notes + ([] if pid else ["No profile identifier in this link."]))

    # ---- /username or /username/posts/<id> ------------------------------
    first = segments[0]
    if first.lower() in RESERVED:
        return _result(raw, "unknown", canonical=_canonical(parsed.path, {k: v[0] for k, v in query.items()}),
                       notes=notes + [f"`{first}` is a Facebook function page, not a profile."])

    if len(segments) >= 3 and segments[1] in {"posts", "photos", "videos", "reels"}:
        return _result(
            raw, "post", username=first, post_id=segments[2],
            canonical=f"https://www.facebook.com/{first}/{segments[1]}/{segments[2]}",
            resolvable=True,
            notes=notes + [f"Pointed at a single post by {first}; the account label is `{first}`."],
        )

    return _result(raw, "profile", username=first,
                   canonical=f"https://www.facebook.com/{first}", resolvable=True, notes=notes)


# --------------------------------------------------------------------------- #
# Cross-checks
# --------------------------------------------------------------------------- #
def _norm_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def compare_owner(link: dict | None, owner_name: str | None) -> dict:
    """Does the pasted link plausibly match the account detected inside the export?"""
    if not link or not link.get("username") or not owner_name:
        return {
            "verdict": "unknown",
            "detail": "No username in the link, or no owner detected in the export — nothing to compare.",
        }
    handle, owner = _norm_name(link["username"]), _norm_name(owner_name)
    if not handle or not owner:
        return {"verdict": "unknown", "detail": "Comparison not possible with these strings."}
    if handle == owner or handle in owner or owner in handle:
        return {
            "verdict": "match",
            "detail": f"The link's username `{link['username']}` is consistent with the export owner `{owner_name}`.",
        }
    return {
        "verdict": "mismatch",
        "detail": (
            f"The link points at `{link['username']}`, but the export's owner (inferred from Messenger "
            f"senders) looks like `{owner_name}`. That often means the wrong archive was uploaded, or that "
            "the username differs from the display name. Worth checking before you trust any finding."
        ),
    }


def link_summary(link: dict | None) -> str:
    """Short human label, e.g. 'profile · @zuck'."""
    if not link:
        return ""
    kind = link.get("kind", "unknown")
    user, num = link.get("username"), link.get("numeric_id")
    if kind == "page" and num:                     # for Pages the numeric ID is the useful bit
        return f"page · {user} (id {num})" if user else f"page · id {num}"
    if user:
        return f"{kind} · @{user}"
    if num:
        return f"{kind} · id {num}"
    return kind
