"""
importers.py
============
Reads a data-subject's **own platform export** — the "Download your information"
file you can request from Instagram/Meta, X/Twitter or Facebook — and converts it
into the app's post schema.

Why exports instead of scraping: the export is authorised by the person the data
is about (GDPR Art. 15 / PDPA access right, or their own download button), needs
no credentials, no evasion, and cannot get anyone's account banned.

Supported shapes
----------------
* X / Twitter archive: `data/tweets.js`, `data/tweets-part*.js` (also inside the .zip)
* Instagram / Meta export: `content/posts_1.json` (also inside the .zip)
* Facebook posts export: `your_posts__check_ins__photos_and_videos_*.json`
* Generic JSON list of already-compatible posts, or CSV (falls through to osint_core)

Privacy note: `data/account.js` in an X archive contains the account's **email**.
This module reads only the `username` field from it and deliberately discards
everything else.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

import osint_core as core

X_TS_FORMAT = "%a %b %d %H:%M:%S %z %Y"

FORMAT_LABELS = {
    "x_archive": "X / Twitter archive",
    "instagram": "Instagram / Meta export",
    "facebook": "Facebook posts export",
    "generic_json": "Generic JSON post list",
    "csv": "CSV",
    "unknown": "Unrecognised",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _strip_js_prefix(text: str) -> str:
    """`window.YTD.tweets.part0 = [ ... ]` -> `[ ... ]`."""
    text = text.lstrip("\ufeff \n\r\t")
    idx = text.find("[")
    return text[idx:] if idx >= 0 else text


def _x_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, X_TS_FORMAT)
    except ValueError:
        return value
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


MENTION_RE = re.compile(r"@([A-Za-z0-9_.]{2,30})")


def _mentions_in_text(text: str) -> list[str]:
    """Caption-embedded @mentions -> tags. This is the only tag signal IG/FB exports carry."""
    seen: list[str] = []
    for handle in MENTION_RE.findall(text or ""):
        tag = "@" + handle
        if tag not in seen:
            seen.append(tag)
    return seen


def _as_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("post") or value.get("text") or value.get("title") or "").strip()
    return ""


# --------------------------------------------------------------------------- #
# Pure mappers -> raw post dicts (then normalised by osint_core)
# --------------------------------------------------------------------------- #
def x_tweets_to_posts(tweets: list[dict]) -> tuple[list[dict], list[str]]:
    posts, notes = [], []
    skipped_replies = 0
    for item in tweets:
        t = item.get("tweet", item)
        text = (t.get("full_text") or t.get("text") or "").strip()
        if not text:
            continue
        entities = t.get("entities") or {}
        mentions = ["@" + m.get("screen_name", "") for m in entities.get("user_mentions", []) if m.get("screen_name")]
        hashtags = ["#" + h.get("text", "") for h in entities.get("hashtags", []) if h.get("text")]
        if t.get("in_reply_to_screen_name"):
            skipped_replies += 1
        posts.append(
            {
                "id": f"x_{t.get('id_str') or t.get('id')}",
                "post_text": text,
                "timestamp": _x_timestamp(t.get("created_at")),
                "location": None,
                "comments": [],          # the archive has no reply threads
                "tags": mentions + hashtags,
                "checkins": [],
                "url": f"https://x.com/i/web/status/{t.get('id_str') or t.get('id')}",
            }
        )
    if skipped_replies:
        notes.append(
            f"{skipped_replies} post(s) are replies to other accounts — kept as posts, but the archive "
            "contains no reply threads, so who replies to the target is not recoverable."
        )
    return posts, notes


def instagram_to_posts(items: list[dict]) -> tuple[list[dict], list[str]]:
    posts, notes = [], []
    unknown_owner = 0
    for item in items:
        media = item.get("media") or []
        first = media[0] if media and isinstance(media[0], dict) else {}
        text = _as_text(item.get("title")) or _as_text(first.get("title"))
        ts = item.get("creation_timestamp") or first.get("creation_timestamp")
        if not text:
            continue  # photo-only posts carry no language signal

        comments = []
        for c in item.get("comments") or []:
            if not isinstance(c, dict):
                continue
            body = _as_text(c.get("string") or c.get("text"))
            if not body:
                continue
            owner = c.get("owner") or c.get("username") or ""
            if not owner:
                unknown_owner += 1
            comments.append(
                {
                    "user": ("@" + str(owner).lstrip("@")) if owner else "@unknown_commenter",
                    "text": body,
                    "timestamp": c.get("submit_timestamp") or c.get("timestamp"),
                }
            )

        tags = [t if str(t).startswith("@") else "@" + str(t) for t in (item.get("tags") or []) if str(t).strip()]
        for tag in _mentions_in_text(text):
            if tag not in tags:
                tags.append(tag)
        posts.append(
            {
                "id": f"ig_{item.get('creation_timestamp') or len(posts)}",
                "post_text": text,
                "timestamp": ts,
                "location": (item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else item.get("location"),
                "comments": comments,
                "tags": tags,
                "checkins": [],
            }
        )
    if unknown_owner:
        notes.append(
            f"{unknown_owner} comment(s) have no account attached in the export; they are grouped as "
            "@unknown_commenter and will look like one very active interactant. Treat that row with care."
        )
    return posts, notes


def facebook_to_posts(items: list[dict]) -> tuple[list[dict], list[str]]:
    posts, notes = [], []
    for item in items:
        text = _as_text(item.get("title")) or _as_text((item.get("data") or [{}])[0])
        if not text:
            continue
        place = item.get("place")
        location = place.get("name") if isinstance(place, dict) else (place if isinstance(place, str) else None)
        tags = []
        for t in item.get("tags") or []:
            name = t.get("name") if isinstance(t, dict) else t
            if name:
                tags.append("@" + str(name).lstrip("@").replace(" ", "_"))
        for tag in _mentions_in_text(text):
            if tag not in tags:
                tags.append(tag)
        posts.append(
            {
                "id": f"fb_{item.get('timestamp')}_{len(posts)}",
                "post_text": text,
                "timestamp": item.get("timestamp"),
                "location": location,
                "comments": [],
                "tags": tags,
                "checkins": [],
            }
        )
    notes.append("The Facebook posts export contains no comment threads, so network analysis is limited.")
    return posts, notes


# --------------------------------------------------------------------------- #
# Detection + extraction
# --------------------------------------------------------------------------- #
def detect_format(filename: str, payload: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".csv"):
        return "csv"
    if name.endswith((".js", ".json")):
        head = payload[:4000].decode("utf-8", errors="replace")
        if "YTD.tweets" in head or "full_text" in head and "created_at" in head:
            return "x_archive"
        if "your_posts__check_ins" in head:
            return "facebook"
        try:
            parsed = json.loads(_strip_js_prefix(payload.decode("utf-8-sig", errors="replace")))
        except Exception:
            return "unknown"
        sample = parsed[0] if isinstance(parsed, list) and parsed else (parsed if isinstance(parsed, dict) else {})
        if isinstance(sample, dict):
            if "tweet" in sample or ("full_text" in sample and "created_at" in sample):
                return "x_archive"
            if "media" in sample or "comments" in sample and "creation_timestamp" in sample:
                return "instagram"
            if "timestamp" in sample and ("title" in sample or "data" in sample):
                return "facebook"
            if any(k in sample for k in ("post_text", "text", "caption")) and "timestamp" in sample:
                return "generic_json"
            if "posts" in sample:
                return "generic_json"
    return "unknown"


def _x_username_from_zip(zf: zipfile.ZipFile) -> str | None:
    """Read only the username from account.js — never the email or phone."""
    for name in zf.namelist():
        if PurePosixPath(name).name in ("account.js", "account.json"):
            try:
                raw = json.loads(_strip_js_prefix(zf.read(name).decode("utf-8-sig", errors="replace")))
                return raw[0]["account"].get("username")
            except Exception:
                return None
    return None


def _posts_from_zip(zf: zipfile.ZipFile) -> tuple[str, list[dict], str | None, list[str]]:
    names = zf.namelist()
    tweets = [n for n in names if re.search(r"(^|/)tweets?(-part\d+)?\.js$", n)]
    instagram = [n for n in names if re.search(r"(^|/)posts_(\d+)?\.json$", n)]
    facebook = [n for n in names if "your_posts__check_ins" in n or re.search(r"facebook/posts/.*\.json$", n)]

    if tweets:
        raw: list[dict] = []
        for n in sorted(tweets):
            raw.extend(json.loads(_strip_js_prefix(zf.read(n).decode("utf-8-sig", errors="replace"))))
        posts, notes = x_tweets_to_posts(raw)
        return "x_archive", posts, _x_username_from_zip(zf), notes
    if instagram:
        raw = []
        for n in sorted(instagram):
            raw.extend(json.loads(zf.read(n).decode("utf-8-sig", errors="replace")))
        posts, notes = instagram_to_posts(raw)
        return "instagram", posts, None, notes
    if facebook:
        raw = []
        for n in sorted(facebook):
            raw.extend(json.loads(zf.read(n).decode("utf-8-sig", errors="replace")))
        posts, notes = facebook_to_posts(raw)
        return "facebook", posts, None, notes
    return "unknown", [], None, ["No posts_*.json, tweets.js or Facebook posts file found inside the archive."]


def import_export(filename: str, payload: bytes) -> tuple[core.Dataset | None, dict]:
    """Detect a platform export and convert it. Falls back to osint_core for
    generic JSON/CSV. Returns (dataset, info)."""
    fmt = detect_format(filename, payload)
    info: dict = {"format": fmt, "label": FORMAT_LABELS.get(fmt, fmt), "notes": [], "author": None}
    raw_posts: list[dict] = []

    try:
        if fmt == "zip":
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                fmt, raw_posts, author, notes = _posts_from_zip(zf)
                info.update(format=fmt, label=FORMAT_LABELS.get(fmt, fmt), author=author, notes=notes)
        elif fmt == "x_archive":
            text = payload.decode("utf-8-sig", errors="replace")
            raw_posts, notes = x_tweets_to_posts(json.loads(_strip_js_prefix(text)))
            info["notes"] = notes
            info["author"] = None
        elif fmt == "instagram":
            raw_posts, notes = instagram_to_posts(json.loads(payload.decode("utf-8-sig", errors="replace")))
            info["notes"] = notes
        elif fmt == "facebook":
            raw_posts, notes = facebook_to_posts(json.loads(payload.decode("utf-8-sig", errors="replace")))
            info["notes"] = notes
        else:
            ds = core.load_upload(filename, payload)
            info.update(format=fmt, label=FORMAT_LABELS.get(fmt, fmt))
            return ds, info
    except (json.JSONDecodeError, KeyError, zipfile.BadZipFile, IndexError) as exc:
        return core.Dataset(posts=[], errors=[f"Could not read this export ({type(exc).__name__}: {exc})."]), info
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
        return core.Dataset(posts=[], errors=[f"Import failed: {exc}"]), info

    if not raw_posts:
        return core.Dataset(posts=[], errors=["The export parsed but contained no posts with text."]), info

    ds = core.normalize_posts(raw_posts, default_author=("@" + info["author"]) if info.get("author") else "@me")
    ds.source_name = f"{info['label']} · {filename}"
    if not info.get("author"):
        info["notes"].append("Account handle was not in the export — set 'Target username' in the sidebar.")
    for note in info["notes"]:
        if note not in ds.warnings:
            ds.warnings.append(note)
    return ds, info
