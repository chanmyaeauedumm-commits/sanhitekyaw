"""
facebook.py
===========
Facebook-only data acquisition. Two authorised routes, no scraping:

A. **"Download your information" export** — the ZIP (or a single JSON) Meta gives
   the account owner. Facebook changes this format regularly, so the scanner is
   structure-tolerant: it walks every JSON in the archive, classifies each file by
   its content, and reports exactly what it recognised and what it ignored.

   Recognised groups:
     · posts / check-ins      (title, timestamp, place, tags)
     · Messenger threads      (participants, sender_name, timestamp_ms, content)
     · your own comments      ("You commented on X's post.")
     · places visited         (name + timestamp -> location events)
     · friends                (name list -> known contacts)

B. **Graph API for a Page you administer** — a Page token with
   `pages_read_engagement` gives the Page feed, comments and places. Personal
   profiles are not accessible this way (Meta removed that in 2018), which is why
   route A exists.

Everything is mapped onto the app's post schema, plus two extra channels that
only Facebook gives you: **Messenger interactions** and **location events**.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

import osint_core as core

USER_AGENT = "digital-dynamics-osint/0.3 (consent-based research tool)"
GRAPH_BASE = "https://graph.facebook.com/v21.0"

KIND_LABELS = {
    "posts": "Timeline posts & check-ins",
    "messages": "Messenger threads",
    "comments": "Your comments on other posts",
    "places": "Places visited",
    "friends": "Friends list",
    "other": "Other / ignored",
}

COMMENT_TARGET_RE = re.compile(r"on\s+(.+?)['’]s\s+(?:post|photo|timeline|profile)", re.I)


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class FacebookExport:
    posts: list[dict] = field(default_factory=list)
    message_events: list[dict] = field(default_factory=list)
    activity_events: list[dict] = field(default_factory=list)
    location_events: list[dict] = field(default_factory=list)
    friends: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    owner: str | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.posts) and not self.errors


# --------------------------------------------------------------------------- #
# Tolerant helpers
# --------------------------------------------------------------------------- #
def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _iso(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def _text_of(item: dict) -> str:
    """Facebook puts the post body in `title`, or nested in data[].post."""
    for key in ("title", "post", "message", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for entry in item.get("data") or []:
        if isinstance(entry, dict):
            value = entry.get("post") or entry.get("comment") or entry.get("text")
            if isinstance(value, str) and value.strip():
                return value.strip()
    for att in item.get("attachments") or []:
        for entry in (att or {}).get("data") or []:
            ctx = (entry or {}).get("external_context") or {}
            if ctx.get("name"):
                return str(ctx["name"]).strip()
    return ""


def _place_of(item: dict) -> str | None:
    for key in ("place", "place_name", "location", "coordinates_label"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nk in ("name", "address", "city"):
                if value.get(nk):
                    return str(value[nk]).strip()
    return None


def _tags_of(item: dict) -> list[str]:
    tags: list[str] = []
    for t in item.get("tags") or item.get("with") or []:
        name = t.get("name") if isinstance(t, dict) else t
        if not name:
            continue
        handle = "@" + str(name).strip().lstrip("@").replace(" ", "_")
        if handle not in tags:
            tags.append(handle)
    return tags


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify(path: str, data) -> str:
    """Classify a file in the export. Shape is trusted over the path, but the path
    breaks ties (comments / friends / likes directories are named consistently)."""
    low = (path or "").lower()

    if isinstance(data, dict):
        if isinstance(data.get("messages"), list) and ("participants" in data or "thread_type" in data):
            return "messages"
        for key in ("posts", "data", "comments"):
            if isinstance(data.get(key), list):
                return classify(low, data[key])
        return "other"

    if not isinstance(data, list) or not data:
        return "other"

    sample = [x for x in data[:25] if isinstance(x, dict)]
    if not sample:
        return "other"

    def share(pred) -> float:
        return sum(1 for x in sample if pred(x)) / len(sample)

    # 1. Messenger
    if share(lambda x: "sender_name" in x and ("timestamp_ms" in x or "timestamp" in x)) > 0.6:
        return "messages"

    # 2. Friends list (name-only rows, or a friends/followers directory)
    if share(lambda x: isinstance(x.get("name"), str)) > 0.8 and share(
        lambda x: set(x.keys()) <= {"name", "timestamp"}
    ) > 0.8:
        return "friends" if ("friend" in low or "follower" in low or share(lambda x: "timestamp" not in x) > 0.8) else "places"
    if ("friend" in low or "follower" in low) and share(lambda x: isinstance(x.get("name"), str)) > 0.8:
        return "friends"

    # 3. Your comments on other people's posts
    if "comment" in low or share(lambda x: "commented" in str(x.get("title", "")).lower()) > 0.5:
        return "comments" if share(lambda x: "timestamp" in x) > 0.5 else "other"

    # 4. Reactions / likes / ads / search history -> ignored on purpose
    if any(k in low for k in ("like", "reaction", "ads", "search", "location_history", "friend_requests")):
        return "other"

    # 5. Posts: timestamp + actual text
    if share(lambda x: "timestamp" in x and bool(_text_of(x))) > 0.5:
        return "posts"

    # 6. Places visited / check-ins
    if share(lambda x: isinstance(x.get("name"), str) and "timestamp" in x) > 0.6:
        return "places"

    return "other"


# --------------------------------------------------------------------------- #
# Per-kind parsing
# --------------------------------------------------------------------------- #
def posts_from_items(items: list[dict], source: str = "") -> tuple[list[dict], list[dict]]:
    """-> (raw posts for the core schema, location events)."""
    posts, places = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _text_of(item)
        ts = _iso(item.get("timestamp") or item.get("creation_timestamp") or item.get("update_timestamp"))
        place = _place_of(item)
        if not text:
            continue
        tags = _tags_of(item)
        checkins = []
        if place and ts:
            checkins.append({"user": "@me", "location": place, "timestamp": ts, "basis": "post_place"})
            # A post that tags someone at a venue is the closest Facebook gets to a
            # joint check-in: both accounts were placed at the same place and time.
            for tag in tags:
                checkins.append({"user": tag, "location": place, "timestamp": ts, "basis": "tagged_post"})
        posts.append(
            {
                "id": f"fb_{ts or len(posts)}_{len(posts)}",
                "post_text": text,
                "timestamp": ts,
                "location": place,
                "tags": tags,
                "comments": [],          # the export never contains other people's replies
                "checkins": checkins,
                "source_file": source,
            }
        )
    return posts, places


def messages_from_thread(thread: dict) -> tuple[list[dict], str | None]:
    """Messenger thread -> interaction events (incoming only)."""
    events: list[dict] = []
    participants = [p.get("name") for p in thread.get("participants") or [] if isinstance(p, dict)]
    title = thread.get("title") or (participants[0] if participants else "")
    for msg in thread.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender_name") or ""
        content = (msg.get("content") or "").strip()
        ts = _iso(msg.get("timestamp_ms") or msg.get("timestamp"))
        if not (sender and ts):
            continue
        events.append(
            {
                "user": "@" + sender.replace(" ", "_"),
                "counterparty_name": sender,
                "timestamp": ts,
                "channel": "messages",
                "thread": title,
                "group": len(participants) > 2,
                "length": len(content),
            }
        )
    return events, title


def activity_from_items(items: list[dict], target_name: str) -> list[dict]:
    """'You commented on X's post.' -> outgoing interaction with X."""
    events: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _text_of(item)
        ts = _iso(item.get("timestamp") or item.get("creation_timestamp"))
        if not (title and ts):
            continue
        match = COMMENT_TARGET_RE.search(title)
        if not match:
            continue
        name = match.group(1).strip()
        if target_name and name.lower() == target_name.lower():
            continue
        events.append(
            {
                "user": "@" + name.replace(" ", "_"),
                "counterparty_name": name,
                "timestamp": ts,
                "channel": "comments_out",
                "thread": title[:80],
            }
        )
    return events


def places_from_items(items: list[dict]) -> list[dict]:
    events = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or _place_of(item)
        ts = _iso(item.get("timestamp") or item.get("creation_timestamp"))
        if name and ts:
            events.append({"user": "@me", "location": str(name).strip(), "timestamp": ts})
    return events


def friends_from_items(items: list[dict]) -> list[str]:
    names = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


# --------------------------------------------------------------------------- #
# Archive scanning
# --------------------------------------------------------------------------- #
def _load_json_bytes(payload: bytes):
    text = payload.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return None
    # Meta sometimes ships JSON wrapped in a JS assignment or with a BOM/prefix.
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
    return json.loads(text[start:] if start > 0 else text)


def scan_export(filename: str, payload: bytes) -> FacebookExport:
    """Walk a Facebook export (ZIP or single JSON) and classify every file."""
    out = FacebookExport()
    blobs: list[tuple[str, object]] = []

    if (filename or "").lower().endswith(".zip") or payload[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith(".json"):
                        continue
                    try:
                        data = _load_json_bytes(zf.read(name))
                    except Exception:
                        out.notes.append(f"Skipped unreadable file: {name}")
                        continue
                    if data is not None:
                        blobs.append((name, data))
        except zipfile.BadZipFile:
            out.errors.append("This ZIP could not be opened — re-download the export from Facebook.")
            return out
    else:
        try:
            data = _load_json_bytes(payload)
        except json.JSONDecodeError as exc:
            out.errors.append(f"Not valid JSON: {exc}")
            return out
        blobs.append((filename or "upload.json", data))

    if not blobs:
        out.errors.append("No JSON files found inside the export.")
        return out

    sender_counter: Counter[str] = Counter()
    for name, data in blobs:
        kind = classify(name, data)
        out.files.append({"path": name, "kind": kind, "label": KIND_LABELS[kind], "entries": len(data) if isinstance(data, list) else 1})

        if kind == "posts":
            posts, places = posts_from_items(data, source=name)
            out.posts.extend(posts)
            out.location_events.extend(places)
        elif kind == "messages":
            threads = data if isinstance(data, list) else [data]
            for thread in threads:
                events, title = messages_from_thread(thread)
                out.message_events.extend(events)
                for e in events:
                    sender_counter[e["counterparty_name"]] += 1
        elif kind == "comments":
            out.activity_events.extend(activity_from_items(data, out.owner or ""))
        elif kind == "places":
            out.location_events.extend(places_from_items(data))
        elif kind == "friends":
            out.friends.extend(friends_from_items(data))

    out.owner = sender_counter.most_common(1)[0][0] if sender_counter else None

    # Drop the owner's own incoming messages: they are 'me', not an interactant.
    if out.owner:
        own = "@" + out.owner.replace(" ", "_")
        out.message_events = [e for e in out.message_events if e["user"] != own]

    recognised = {f["kind"] for f in out.files if f["kind"] != "other"}
    if "posts" not in recognised:
        out.notes.append(
            "No posts file was recognised in this export. In Facebook's export dialog, make sure "
            "'Posts' is selected (JSON format, not HTML)."
        )
    if not recognised:
        out.notes.append("No recognisable Facebook files found — the export dialog may have been set to HTML only.")
    return out


# --------------------------------------------------------------------------- #
# Build the app dataset
# --------------------------------------------------------------------------- #
def build_dataset(
    export: FacebookExport,
    include_messages: bool = True,
    target: str = "",
    source_name: str = "",
) -> tuple[core.Dataset, dict]:
    """FacebookExport -> core.Dataset (+ extras channels) and a provenance dict."""
    target_handle = core.normalize_handle(target) if target else (
        "@" + export.owner.replace(" ", "_") if export.owner else "@me"
    )

    # Posts are attributed to the target; check-ins carry the same handle.
    for post in export.posts:
        post["author"] = target_handle
        for ck in post.get("checkins") or []:
            # only the placeholder owner is re-attributed; tagged friends keep their handle
            if ck.get("user") in ("@me", "", None):
                ck["user"] = target_handle

    ds = core.normalize_posts(export.posts, default_author=target_handle)
    ds.source_name = source_name or "Facebook export"

    for ev in export.location_events:
        if ev.get("user") in ("@me", "", None):
            ev["user"] = target_handle

    extras = {
        "message_events": export.message_events if include_messages else [],
        "activity_events": export.activity_events,
        "location_events": export.location_events,
        "known_contacts": export.friends,
        "messages_included": include_messages,
    }
    ds.extras = extras

    info = {
        "platform": "facebook",
        "owner_detected": export.owner,
        "target_handle": target_handle,
        "files": export.files,
        "posts": len(export.posts),
        "messages": len(export.message_events),
        "activity_events": len(export.activity_events),
        "locations": len(export.location_events),
        "friends": len(export.friends),
        "messages_included": include_messages,
    }

    if export.message_events and not include_messages:
        ds.warnings.append(
            f"{len(export.message_events)} Messenger interaction(s) were found but excluded "
            "(Messenger analysis is switched off)."
        )
    if not export.message_events:
        ds.warnings.append(
            "No Messenger threads found. Facebook only includes them if 'Messages' was ticked "
            "when the export was requested."
        )
    if export.posts:
        ds.warnings.append(
            "Facebook exports contain no other people's comments, so the interaction network is built "
            "from Messenger and your own comments — not from your timeline replies."
        )
    if export.friends:
        ds.warnings.append(
            f"{len(export.friends)} friends imported as *known contacts* — used only to label "
            "interactants, never to infer anything on its own."
        )
    return ds, info


def import_export(filename: str, payload: bytes, include_messages: bool = True, target: str = "") -> tuple[core.Dataset | None, dict]:
    export = scan_export(filename, payload)
    if export.errors:
        return core.Dataset(posts=[], errors=export.errors, source_name=filename), {"platform": "facebook"}
    ds, info = build_dataset(export, include_messages=include_messages, target=target, source_name=f"Facebook export · {filename}")
    ds.warnings.extend([n for n in export.notes if n not in ds.warnings])
    info["notes"] = list(export.notes)
    return ds, info


# --------------------------------------------------------------------------- #
# Route B · Graph API for a Page you administer
# --------------------------------------------------------------------------- #
def graph_to_posts(feed: list[dict], comments_by_post: dict[str, list[dict]] | None = None, page_handle: str = "@page") -> list[dict]:
    """Pure mapping for the Graph API (testable without network)."""
    comments_by_post = comments_by_post or {}
    posts = []
    for item in feed:
        text = (item.get("message") or item.get("story") or "").strip()
        ts = _iso(item.get("created_time"))
        if not text or not ts:
            continue
        place = (item.get("place") or {}).get("name") if isinstance(item.get("place"), dict) else item.get("place")
        comments = []
        for c in comments_by_post.get(str(item.get("id")), []):
            body = (c.get("message") or "").strip()
            if not body:
                continue
            name = (c.get("from") or {}).get("name") or "unknown"
            comments.append(
                {
                    "user": "@" + str(name).replace(" ", "_"),
                    "text": body,
                    "timestamp": _iso(c.get("created_time")),
                }
            )
        posts.append(
            {
                "id": f"fbgraph_{item.get('id')}",
                "author": page_handle,
                "post_text": text,
                "timestamp": ts,
                "location": place,
                "comments": comments,
                "tags": [],
                "checkins": [{"user": page_handle, "location": place, "timestamp": ts}] if place else [],
                "url": item.get("permalink_url"),
            }
        )
    return posts


def graph_fetch(page_id: str, access_token: str, limit: int = 50, max_threads: int = 20, delay: float = 0.5) -> tuple[list[dict], list[str], list[str], dict]:
    """Page feed + comments. Needs a Page token with pages_read_engagement."""
    import time

    errors: list[str] = []
    warnings = [
        "Graph API only exposes Pages you administer — personal profiles are not accessible this way.",
        "Facebook exposes no check-in venue for most Page posts, so co-location is usually empty.",
    ]

    def get(url: str):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    base = (
        f"{GRAPH_BASE}/{urllib.parse.quote(page_id)}/feed"
        f"?fields=id,message,story,created_time,permalink_url,place"
        f"&limit={min(limit, 100)}&access_token={urllib.parse.quote(access_token)}"
    )
    try:
        feed = get(base).get("data", [])
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("error", {}).get("message", "")
        except Exception:
            pass
        errors.append(f"Graph API {exc.code}: {detail or exc.reason}")
        return [], warnings, errors, {}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Graph API request failed: {exc}")
        return [], warnings, errors, {}

    comments_by_post: dict[str, list[dict]] = {}
    for item in feed[:max_threads]:
        pid = str(item.get("id"))
        try:
            url = (
                f"{GRAPH_BASE}/{urllib.parse.quote(pid)}/comments"
                f"?fields=message,created_time,from&limit=100&access_token={urllib.parse.quote(access_token)}"
            )
            comments_by_post[pid] = get(url).get("data", [])
        except Exception:
            pass
        time.sleep(delay)

    posts = graph_to_posts(feed, comments_by_post, page_handle="@" + page_id)
    meta = {"platform": "facebook-graph", "page": page_id, "posts": len(posts), "threads": len(comments_by_post)}
    return posts, warnings, errors, meta
