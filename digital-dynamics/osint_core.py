"""
osint_core.py
=============
Deterministic analysis layer: loading, normalising, aggregating, and turning
patterns into *transparent possibility* findings.

Design rule: every number a finding states must come from the data itself.
This module never guesses. It produces Evidence + Interpretation separately,
and it never asserts a conclusion as fact.

Schema (per post) — JSON or CSV
-------------------------------
{
  "id": "post_001",                       # optional
  "author": "@target",                    # optional, defaults to target_username
  "post_text": "caption text",            # required
  "timestamp": "2026-03-04T20:12:00",     # required (ISO 8601 or unix seconds)
  "location": "Blue Door Cafe",           # optional
  "comments": [{"user": "@a", "text": "...", "timestamp": "..."}],   # optional
  "tags": ["@a", "@b"],                   # optional
  "checkins": [{"user": "@a", "location": "Blue Door Cafe",
                "timestamp": "..."}]      # optional, enables co-location analysis
}
"""

from __future__ import annotations

import io
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

import pandas as pd

try:  # optional, but installed by requirements.txt
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    _VADER = SentimentIntensityAnalyzer()
except Exception:  # pragma: no cover
    _VADER = None

DISCLAIMER = (
    "Based only on the uploaded public digital footprint. Absence of evidence is "
    "not evidence of absence: missing posts, deleted content, private accounts, "
    "DMs and offline context are all invisible here."
)

NIGHT_HOURS = {22, 23, 0, 1, 2, 3, 4}
SOLO_LEXICON = {
    "alone", "solo", "myself", "quiet", "independent", "unplugged", "me time",
    "my own", "no plans", "silence", "empty", "lighter", "detach", "withdrawn",
}
SOCIAL_LEXICON = {
    "we", "us", "our", "together", "everyone", "crew", "team", "squad", "friends",
    "family", "group", "party", "celebration",
}
VAGUE_LEXICON = {
    "things", "something", "somewhere", "somehow", "not everything", "no explanation",
    "thinking", "processing", "figuring", "maybe", "not posting", "quiet",
}


# --------------------------------------------------------------------------- #
# Loading & normalising
# --------------------------------------------------------------------------- #
@dataclass
class Dataset:
    posts: list[dict]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_name: str = ""
    # Optional extra channels (Facebook only): Messenger interactions, outgoing
    # comments, standalone location events, known contacts.
    extras: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.posts) and not self.errors


def _parse_ts(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and len(text) in (10, 13):
        v = int(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(v)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = pd.to_datetime(text, errors="raise").to_pydatetime()
        except Exception:
            return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _as_list(value: Any) -> list:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[0] in "[{":
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                pass
        return [p.strip() for p in re.split(r"[;,|]", text) if p.strip()]
    return [value]


def _norm_handle(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith(("@", "#")):   # hashtags stay hashtags
        return text
    return f"@{text}"


def normalize_handle(value: Any) -> str:
    """Public wrapper: 'name' / '@name' / 'u/name' -> '@name'."""
    text = str(value or "").strip()
    if text.lower().startswith("u/"):
        text = text[2:]
    return _norm_handle(text)


def _norm_comment(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"user": _norm_handle(raw.split(":")[0]) if ":" in raw else "", "text": raw, "timestamp": None}
    if not isinstance(raw, dict):
        return {"user": "", "text": str(raw), "timestamp": None}
    user = raw.get("user") or raw.get("username") or raw.get("author") or raw.get("commenter") or ""
    text = raw.get("text") or raw.get("comment") or raw.get("body") or raw.get("message") or ""
    return {
        "user": _norm_handle(user),
        "text": str(text),
        "timestamp": _parse_ts(raw.get("timestamp") or raw.get("created_at") or raw.get("time")),
    }


def _norm_checkin(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    user = _norm_handle(raw.get("user") or raw.get("username") or raw.get("author") or "")
    location = str(raw.get("location") or raw.get("place") or "").strip()
    ts = _parse_ts(raw.get("timestamp") or raw.get("created_at") or raw.get("time"))
    if not (user and location and ts):
        return None
    out = {"user": user, "location": location, "timestamp": ts}
    if raw.get("basis"):           # e.g. "tagged_post" for Facebook tag-based presence
        out["basis"] = raw["basis"]
    return out


def normalize_posts(raw_posts: Iterable[Any], default_author: str = "") -> Dataset:
    posts: list[dict] = []
    warnings: list[str] = []
    skipped = 0

    for idx, raw in enumerate(raw_posts):
        if not isinstance(raw, dict):
            skipped += 1
            continue

        text = raw.get("post_text") or raw.get("text") or raw.get("caption") or raw.get("content") or ""
        ts = _parse_ts(raw.get("timestamp") or raw.get("created_at") or raw.get("date") or raw.get("time"))
        if ts is None or not str(text).strip():
            skipped += 1
            continue

        location = raw.get("location") or raw.get("place") or raw.get("venue") or ""
        location = str(location).strip() or None

        author = _norm_handle(raw.get("author") or raw.get("username") or default_author) or "@target"

        comments = [c for c in (_norm_comment(x) for x in _as_list(raw.get("comments"))) if c["text"]]
        tags = [_norm_handle(t) for t in _as_list(raw.get("tags")) if str(t).strip()]

        checkins = [c for c in (_norm_checkin(x) for x in _as_list(raw.get("checkins") or raw.get("check_ins"))) if c]
        if location and not any(c["user"] == author for c in checkins):
            checkins.append({"user": author, "location": location, "timestamp": ts})

        posts.append(
            {
                "id": str(raw.get("id") or raw.get("post_id") or f"row_{idx + 1}"),
                "author": author,
                "text": str(text).strip(),
                "timestamp": ts,
                "location": location,
                "comments": comments,
                "tags": tags,
                "checkins": checkins,
            }
        )

    if skipped:
        warnings.append(f"{skipped} row(s) skipped: missing a parseable timestamp or post text.")
    if len(posts) < 5:
        warnings.append(
            "Fewer than 5 usable posts — statistical patterns (routines, shifts) are not meaningful yet."
        )
    posts.sort(key=lambda p: p["timestamp"])
    return Dataset(posts=posts, warnings=warnings, source_name="")


def load_upload(filename: str, payload: bytes) -> Dataset:
    """Parse an uploaded JSON or CSV file into a Dataset."""
    name = (filename or "").lower()
    text = payload.decode("utf-8-sig", errors="replace")

    if name.endswith(".json"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return Dataset(posts=[], errors=[f"Invalid JSON: {exc}"], source_name=filename)
        if isinstance(parsed, dict):
            for key in ("posts", "data", "items", "results", "records"):
                if isinstance(parsed.get(key), list):
                    parsed = parsed[key]
                    break
        if not isinstance(parsed, list):
            return Dataset(
                posts=[],
                errors=["JSON must be a list of post objects (or {\"posts\": [...]})."],
                source_name=filename,
            )
        ds = normalize_posts(parsed)
    elif name.endswith(".csv"):
        try:
            df = pd.read_csv(io.StringIO(text))
        except Exception as exc:
            return Dataset(posts=[], errors=[f"Invalid CSV: {exc}"], source_name=filename)
        ds = normalize_posts(df.to_dict("records"))
    else:
        return Dataset(posts=[], errors=["Unsupported file type. Upload a .json or .csv file."], source_name=filename)

    ds.source_name = filename
    return ds


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def time_bucket(dt: datetime) -> str:
    h = dt.hour
    if 5 <= h < 12:
        return "Morning (05–11)"
    if 12 <= h < 17:
        return "Afternoon (12–16)"
    if 17 <= h < 22:
        return "Evening (17–21)"
    return "Night (22–04)"


def sentiment(text: str) -> float:
    if _VADER is None:
        positives = sum(1 for w in re.findall(r"[a-z']+", text.lower()) if w in {"love", "great", "good", "happy", "fun", "best", "proud", "laughing"})
        negatives = sum(1 for w in re.findall(r"[a-z']+", text.lower()) if w in {"tired", "sad", "bad", "hate", "alone", "gone", "lost"})
        total = positives + negatives
        return 0.0 if total == 0 else (positives - negatives) / total
    return float(_VADER.polarity_scores(text)["compound"])


def _lex_hits(text: str, lexicon: set[str]) -> int:
    low = f" {text.lower()} "
    return sum(1 for term in lexicon if f" {term} " in low or f" {term}," in low or low.strip().startswith(f"{term} ") or f" {term}." in low)


def hour_str(h: float) -> str:
    """17.29 -> '17:18'."""
    total = int(round(h * 60)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _era_split(posts: list[dict], recent_days: int) -> tuple[list[dict], list[dict], datetime]:
    last_ts = max(p["timestamp"] for p in posts)
    cutoff = last_ts - timedelta(days=recent_days)
    older = [p for p in posts if p["timestamp"] < cutoff]
    recent = [p for p in posts if p["timestamp"] >= cutoff]
    return older, recent, cutoff


def _profile(posts: list[dict], target: str) -> dict:
    """Compact behavioural profile for one window of posts."""
    if not posts:
        return {}
    n = len(posts)
    hours = [p["timestamp"].hour for p in posts]
    comment_counts = Counter(c["user"] for p in posts for c in p["comments"] if c["user"] and c["user"] != target)
    loc_counts = Counter(p["location"] for p in posts if p["location"])
    sentiments = [sentiment(p["text"]) for p in posts]
    words = [len(p["text"].split()) for p in posts]
    solo = [_lex_hits(p["text"], SOLO_LEXICON) for p in posts]
    social = [_lex_hits(p["text"], SOCIAL_LEXICON) for p in posts]
    vague = [_lex_hits(p["text"], VAGUE_LEXICON) for p in posts]
    return {
        "posts": n,
        "avg_hour": round(sum(hours) / n, 2),
        "night_share": round(sum(1 for h in hours if h in NIGHT_HOURS) / n, 3),
        "commenters": dict(comment_counts.most_common(10)),
        "locations": dict(loc_counts.most_common(10)),
        "avg_sentiment": round(sum(sentiments) / n, 3),
        "avg_words": round(sum(words) / n, 1),
        "solo_rate": round(sum(1 for s in solo if s) / n, 3),
        "social_rate": round(sum(1 for s in social if s) / n, 3),
        "vague_rate": round(sum(1 for v in vague if v) / n, 3),
        "first_ts": posts[0]["timestamp"].isoformat(timespec="minutes"),
        "last_ts": posts[-1]["timestamp"].isoformat(timespec="minutes"),
    }


def co_location_events(posts: list[dict], target: str, window_hours: float = 3.0) -> list[dict]:
    """Same venue, within `window_hours`. Compared on absolute time so a check-in
    at 23:40 and one at 00:20 still count as the same evening."""
    by_location: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        for ck in p["checkins"]:
            by_location[ck["location"]].append(ck)

    episodes: list[dict] = []
    for location, events in by_location.items():
        target_events = [e for e in events if e["user"] == target]
        others = [e for e in events if e["user"] != target]
        if not target_events or not others:
            continue
        for other in others:
            nearest = min(
                target_events,
                key=lambda t: abs((other["timestamp"] - t["timestamp"]).total_seconds()),
            )
            gap = (other["timestamp"] - nearest["timestamp"]).total_seconds() / 3600
            if abs(gap) <= window_hours:
                episodes.append(
                    {
                        "user": other["user"],
                        "location": location,
                        "date": str(nearest["timestamp"].date()),
                        "target_ts": nearest["timestamp"],
                        "other_ts": other["timestamp"],
                        "gap_hours": round(abs(gap), 2),
                        "direction": "after" if gap >= 0 else "before",
                        "basis": other.get("basis", "checkin"),
                    }
                )
    episodes.sort(key=lambda e: e["target_ts"])
    return episodes


def _prep_events(events: list[dict] | None) -> list[dict]:
    out = []
    for ev in events or []:
        ts = ev.get("timestamp")
        dt = ts if isinstance(ts, datetime) else _parse_ts(ts)
        if dt is None or not ev.get("user"):
            continue
        out.append({**ev, "timestamp": dt})
    return out


def _channel_profile(events: list[dict], cutoff: datetime | None = None, recent: bool | None = None) -> dict:
    """Compact profile for a non-post channel (Messenger, outgoing comments)."""
    if recent is not None and cutoff is not None:
        events = [e for e in events if (e["timestamp"] >= cutoff) == recent]
    if not events:
        return {"events": 0, "partners": 0, "avg_hour": None, "night_share": None,
                "top_partners": [], "span_days": 0}
    hours = [e["timestamp"].hour for e in events]
    counts = Counter(e["user"] for e in events)
    first, last = min(e["timestamp"] for e in events), max(e["timestamp"] for e in events)
    return {
        "events": len(events),
        "partners": len(counts),
        "avg_hour": round(sum(hours) / len(hours), 2),
        "night_share": round(sum(1 for h in hours if h in NIGHT_HOURS) / len(events), 3),
        "top_partners": counts.most_common(10),
        "span_days": max((last - first).days, 0),
        "first": first.isoformat(timespec="minutes"),
        "last": last.isoformat(timespec="minutes"),
    }


def build_metrics(posts: list[dict], target: str, recent_days: int = 90, extras: dict | None = None) -> dict:
    target = _norm_handle(target) if target else (Counter(p["author"] for p in posts).most_common(1)[0][0])
    older, recent, cutoff = _era_split(posts, recent_days)

    commenter_totals = Counter(c["user"] for p in posts for c in p["comments"] if c["user"])
    loc_totals = Counter(p["location"] for p in posts if p["location"])
    bucket_counts = Counter(time_bucket(p["timestamp"]) for p in posts)

    monthly = Counter(p["timestamp"].strftime("%Y-%m") for p in posts)
    monthly_interactions = Counter(
        p["timestamp"].strftime("%Y-%m") for p in posts for _ in p["comments"]
    )

    episodes = co_location_events(posts, target)
    episode_counts = Counter(e["user"] for e in episodes)

    clusters: list[dict] = []
    per_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in episodes:
        per_pair[(e["user"], e["location"])].append(e)
    for (user, venue), items in per_pair.items():
        if len(items) >= 2:
            dates = sorted({i["date"] for i in items})
            clusters.append(
                {
                    "user": user,
                    "venue": venue,
                    "count": len(items),
                    "first": dates[0],
                    "last": dates[-1],
                    "spread_days": (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).days,
                    "avg_gap_hours": round(sum(i["gap_hours"] for i in items) / len(items), 2),
                    "basis": Counter(i.get("basis", "checkin") for i in items).most_common(1)[0][0],
                }
            )
    clusters.sort(key=lambda c: (-c["count"], c["spread_days"]))

    # ---- extra channels (Facebook) ----------------------------------------
    extras = extras or {}
    messages = _prep_events(extras.get("message_events"))
    outgoing = _prep_events(extras.get("activity_events"))
    extra_locations = _prep_events(extras.get("location_events"))
    known_contacts = {str(n).lower() for n in extras.get("known_contacts") or [] if n}

    extra_loc_counts = Counter(ev["location"] for ev in extra_locations if ev.get("location"))
    loc_totals.update(extra_loc_counts)

    metrics = {
        "target": target,
        "n_posts": len(posts),
        "n_comments": sum(len(p["comments"]) for p in posts),
        "span": {
            "first": posts[0]["timestamp"].isoformat(timespec="minutes"),
            "last": posts[-1]["timestamp"].isoformat(timespec="minutes"),
            "days": (posts[-1]["timestamp"] - posts[0]["timestamp"]).days,
        },
        "top_commenters": commenter_totals.most_common(10),
        "top_locations": loc_totals.most_common(10),
        "time_buckets": {b: bucket_counts.get(b, 0) for b in ["Morning (05–11)", "Afternoon (12–16)", "Evening (17–21)", "Night (22–04)"]},
        "monthly_volume": dict(sorted(monthly.items())),
        "monthly_comments": dict(sorted(monthly_interactions.items())),
        "sentiment_series": [
            {"date": p["timestamp"].date().isoformat(), "sentiment": round(sentiment(p["text"]), 3)}
            for p in posts
        ],
        "overall": _profile(posts, target),
        "recent_window_days": recent_days,
        "recent_cutoff": cutoff.date().isoformat(),
        "earlier": _profile(older, target),
        "recent": _profile(recent, target),
        "co_location": {
            "episodes": episode_counts.most_common(10),
            "clusters": clusters,
            "total": len(episodes),
            "details": [
                {
                    "user": e["user"],
                    "location": e["location"],
                    "date": e["date"],
                    "target_ts": e["target_ts"].isoformat(timespec="minutes"),
                    "other_ts": e["other_ts"].isoformat(timespec="minutes"),
                    "gap_hours": e["gap_hours"],
                    "basis": e.get("basis", "checkin"),
                }
                for e in episodes
            ][:200],
        },
        "channels": {
            "posts": len(posts),
            "comments_received": sum(len(p["comments"]) for p in posts),
            "comments_given": len(outgoing),
            "messages": len(messages),
            "location_events": len(extra_locations),
            "messages_included": bool(extras.get("messages_included", bool(messages))),
        },
        "messages": {
            "overall": _channel_profile(messages),
            "earlier": _channel_profile(messages, cutoff, False),
            "recent": _channel_profile(messages, cutoff, True),
        },
        "outgoing_comments": _channel_profile(outgoing),
        "known_contacts": sorted(known_contacts),
        "top_interactants": _top_interactants(posts, messages, outgoing, target),
        "raw_posts": posts,
    }
    return metrics


def _top_interactants(posts: list[dict], messages: list[dict], outgoing: list[dict], target: str) -> list[dict]:
    """Comments received + messages + comments given, per account, with channel split."""
    rows: dict[str, dict] = {}
    for p in posts:
        for c in p["comments"]:
            if c["user"] and c["user"] != target:
                row = rows.setdefault(c["user"], {"account": c["user"], "comments_in": 0, "messages": 0, "comments_out": 0})
                row["comments_in"] += 1
    for ev in messages:
        if ev["user"] != target:
            row = rows.setdefault(ev["user"], {"account": ev["user"], "comments_in": 0, "messages": 0, "comments_out": 0})
            row["messages"] += 1
    for ev in outgoing:
        if ev["user"] != target:
            row = rows.setdefault(ev["user"], {"account": ev["user"], "comments_in": 0, "messages": 0, "comments_out": 0})
            row["comments_out"] += 1
    for row in rows.values():
        row["total"] = row["comments_in"] + row["messages"] + row["comments_out"]
    return sorted(rows.values(), key=lambda r: -r["total"])[:12]


# --------------------------------------------------------------------------- #
# Findings → "Transparent Possibility" objects
# --------------------------------------------------------------------------- #
def _confidence(score: float) -> str:
    if score >= 0.75:
        return "High"
    if score >= 0.45:
        return "Medium"
    return "Low"


def _finding(
    key: str,
    category: str,
    title: str,
    fact: str,
    reasoning: str,
    possibility: str,
    confidence: str,
    **evidence: Any,
) -> dict:
    return {
        "key": key,
        "category": category,
        "title": title,
        "data_fact": fact,
        "logical_reasoning": reasoning,
        "possibility": possibility,
        "confidence": confidence,
        "disclaimer": DISCLAIMER,
        "evidence": evidence,
    }


def detect_findings(m: dict) -> list[dict]:
    """Rule-based findings. Thresholds are explicit so nothing is hand-waved."""
    out: list[dict] = []
    n = m["n_posts"]
    target = m["target"]
    earlier, recent = m["earlier"], m["recent"]

    # 1. Core network --------------------------------------------------------
    if m["top_commenters"]:
        user, count = m["top_commenters"][0]
        share = count / max(m["n_comments"], 1)
        active_months = sum(
            1 for mo in m["monthly_comments"].values() if mo > 0
        )
        conf = _confidence(min(1.0, share * 1.6 + (0.2 if count >= 10 else 0)))
        out.append(
            _finding(
                "core_network",
                "Core Network",
                f"Primary interactant: {user}",
                f"{user} appears in {count} of {m['n_comments']} comments ({share:.0%}) across "
                f"{active_months} active month(s), 1st of {len(m['top_commenters'])} recurring commenters.",
                "A single account dominating the comment thread is the strongest available proxy for "
                "tie strength: repeated, low-effort, high-frequency replies usually mean habitual "
                "contact rather than one-off interest.",
                "This pattern suggests a close, sustained relationship (close friend, family member "
                "or partner). The comment volume alone cannot distinguish which — tone, shared "
                "in-jokes and reciprocal tagging would be needed to narrow it.",
                conf,
                user=user,
                count=count,
                share=round(share, 3),
                runner_up=m["top_commenters"][1] if len(m["top_commenters"]) > 1 else None,
            )
        )

    # 1b. Messenger channel (Facebook only) --------------------------------
    msg = m.get("messages") or {}
    overall_msg = msg.get("overall") or {}
    if overall_msg.get("events", 0) >= 10:
        partner, count = overall_msg["top_partners"][0]
        share = count / overall_msg["events"]
        in_friends = partner.lstrip("@").replace("_", " ").lower() in (m.get("known_contacts") or [])
        out.append(
            _finding(
                "message_network",
                "Core Network",
                f"Primary Messenger contact: {partner}",
                f"{count} incoming messages from {partner} "
                f"({share:.0%} of {overall_msg['events']} incoming messages across "
                f"{overall_msg['partners']} conversation partner(s), "
                f"{overall_msg['span_days']} days of history). "
                f"{'This account is in the friend list.' if in_friends else 'This account is NOT in the exported friend list.'}",
                "A Messenger thread is a private, deliberate channel: unlike a public comment it requires "
                "choosing the person. Sustained volume in one thread is a stronger tie signal than any "
                "public interaction, and its absence for someone else is equally informative.",
                f"This suggests an active private line of contact with {partner}. The export cannot say "
                "whether the relationship is close, practical (work, family logistics) or new — message "
                "content and reciprocity would be needed, and both are outside what this tool measures.",
                _confidence(0.45 + min(share, 0.7) * 0.6),
                partner=partner,
                messages=count,
                share=round(share, 3),
                partners=overall_msg["partners"],
                in_friends=in_friends,
            )
        )

        recent_msg, earlier_msg = msg.get("recent") or {}, msg.get("earlier") or {}
        if recent_msg.get("events", 0) >= 5 and earlier_msg.get("events", 0) >= 5:
            e_map = dict(earlier_msg["top_partners"])
            r_map = dict(recent_msg["top_partners"])
            e_total = sum(e_map.values()) or 1
            r_total = sum(r_map.values()) or 1
            rising, falling = [], []
            for user in set(e_map) | set(r_map):
                e_n, r_n = e_map.get(user, 0), r_map.get(user, 0)
                e_s, r_s = e_n / e_total, r_n / r_total
                if r_n >= 5 and r_s >= 1.8 * max(e_s, 0.01):
                    rising.append((user, e_n, r_n))
                elif e_n >= 5 and e_s >= 1.8 * max(r_s, 0.01):
                    falling.append((user, e_n, r_n))
            if rising or falling:
                bits = []
                if rising:
                    bits.append("appeared: " + ", ".join(f"{u} ({a} → {b})" for u, a, b in sorted(rising, key=lambda x: -x[2])))
                if falling:
                    bits.append("faded: " + ", ".join(f"{u} ({a} → {b})" for u, a, b in sorted(falling, key=lambda x: -x[1])))
                out.append(
                    _finding(
                        "message_shift",
                        "Routine Shift",
                        "Messenger network rotated",
                        "Incoming messages per partner, earlier window → last "
                        f"{m['recent_window_days']} days: " + "; ".join(bits) + ".",
                        "Private threads are where contact actually happens. A partner appearing or "
                        "disappearing at this volume is a change in who the account talks to, not "
                        "background noise.",
                        "This indicates the private social circle shifted during the observation period. "
                        "It does not establish why: a new job, a move, a new relationship and simply "
                        "switching apps all produce this shape.",
                        _confidence(0.4 + 0.15 * min(len(rising) + len(falling), 3)),
                        rising=[{"user": u, "earlier": a, "recent": b} for u, a, b in rising],
                        falling=[{"user": u, "earlier": a, "recent": b} for u, a, b in falling],
                    )
                )

            night_delta = (recent_msg.get("night_share") or 0) - (earlier_msg.get("night_share") or 0)
            if abs(night_delta) >= 0.2:
                out.append(
                    _finding(
                        "message_night_shift",
                        "Routine Shift",
                        "Messaging hours moved",
                        f"Night share of incoming messages (22:00–04:59): "
                        f"{(earlier_msg.get('night_share') or 0):.0%} → {(recent_msg.get('night_share') or 0):.0%}. "
                        f"Mean hour {hour_str(earlier_msg['avg_hour'])} → {hour_str(recent_msg['avg_hour'])}.",
                        "Messaging time is closer to actual daily rhythm than posting time, which can be "
                        "scheduled or batched.",
                        "This suggests the account's waking or available hours shifted. It says nothing "
                        "about who they were talking to or why.",
                        _confidence(0.4 + min(abs(night_delta), 0.4)),
                        earlier_night=earlier_msg.get("night_share"),
                        recent_night=recent_msg.get("night_share"),
                    )
                )

    # 1c. Known-contact cross-check (Facebook friend list) -----------------
    if m.get("known_contacts") and m.get("top_interactants"):
        known = set(m["known_contacts"])
        outsiders = []
        for row in m.get("top_interactants", [])[:5]:
            name = row["account"].lstrip("@").replace("_", " ").lower()
            if name not in known and row["total"] >= 5:
                outsiders.append(row)
        if outsiders:
            top = outsiders[0]
            listed = ", ".join(f"{o['account']} ({o['total']} interactions)" for o in outsiders[:3])
            out.append(
                _finding(
                    "non_friend_contact",
                    "Anomaly",
                    "High-contact accounts outside the friend list",
                    f"{len(outsiders)} of the top 5 interactants missing from the {len(known)} exported "
                    f"friends, led by {top['account']}: {listed}.",
                    "The friend list is a coarse map of the account's stated social circle. Sustained "
                    "contact with someone outside it is either a new or unconfirmed connection, a name "
                    "mismatch (renamed, deactivated or unicode-variant accounts), or contact kept "
                    "deliberately off the friends list.",
                    "This may indicate relationships the friend list does not reflect. Matching here is "
                    "naive string comparison, so an accent, nickname or rename produces this flag with no "
                    "social meaning at all — check the account before reading anything into it.",
                    "Low",
                    accounts=[o["account"] for o in outsiders],
                    known_contacts=len(known),
                )
            )

    # 2. Co-location ---------------------------------------------------------
    col = m["co_location"]
    if col["episodes"]:
        cluster = col["clusters"][0] if col["clusters"] else None
        if cluster:
            user, venue, count = cluster["user"], cluster["venue"], cluster["count"]
            spread = cluster["spread_days"]
            total_for_user = dict(col["episodes"]).get(user, count)
            tagged = cluster.get("basis") == "tagged_post"
            if tagged:
                fact = (
                    f"{count} post(s) between {cluster['first']} and {cluster['last']} in which {target} "
                    f"tagged {user} at {venue}, spanning {spread} day(s). Across all venues, {user} is "
                    f"co-located in tags {total_for_user} time(s)."
                )
                reasoning = (
                    "A tag at a venue is the account owner asserting shared presence, which is stronger "
                    "than two independent check-ins that merely coincide — repetition at one venue is "
                    "what turns a single outing into a routine."
                )
            else:
                fact = (
                    f"{count} same-day check-ins at {venue} within 3 hours of each other by {target} and "
                    f"{user}, spanning {spread} day(s) "
                    f"({cluster['first']} → {cluster['last']}), average gap "
                    f"{cluster['avg_gap_hours']}h. Across all venues, {user} overlaps {total_for_user} time(s)."
                )
                reasoning = (
                    "A single overlap is coincidence; repeated overlaps at one venue is scheduling. The "
                    "venue count and the tight date spread are what carry the signal here, not the raw total."
                )
            tight = count >= 3 and spread <= 21
            possibility = (
                f"This indicates a possible shared routine or in-person contact around {venue}. It does "
                "not establish what the relationship is — a tag can be added long after the fact, the same "
                "venue can serve two unconnected routines, and living nearby produces the same pattern."
            )
            conf = _confidence(
                0.3
                + 0.15 * min(count, 4)
                + (0.15 if spread <= 21 else 0)
                + (0.1 if cluster["avg_gap_hours"] <= 1.5 else 0)
                + (0.05 if tagged else 0)
            )
            out.append(
                _finding(
                    "co_location",
                    "Location Overlap",
                    f"Repeated co-location with {user} at {venue}",
                    fact,
                    reasoning,
                    possibility,
                    conf,
                    user=user,
                    venue=venue,
                    episodes=count,
                    spread_days=spread,
                    total_for_user=total_for_user,
                    avg_gap_hours=cluster["avg_gap_hours"],
                    details=[d for d in col["details"] if d["user"] == user and d["location"] == venue][:8],
                )
            )
        else:
            user, count = col["episodes"][0]
            out.append(
                _finding(
                    "co_location_single",
                    "Location Overlap",
                    f"Isolated overlap with {user}",
                    f"{count} same-day, same-venue check-in(s) within 3 hours with {user}; no venue repeats.",
                    "One-off overlaps are common in dense areas or among people with overlapping commutes.",
                    "This is not yet a pattern. Treat it as a lead to verify, not as evidence of contact.",
                    "Low",
                    user=user,
                    episodes=count,
                )
            )

    # 3. Routine shift -------------------------------------------------------
    if earlier and recent and earlier["posts"] >= 5 and recent["posts"] >= 5:
        hour_delta = recent["avg_hour"] - earlier["avg_hour"]
        if abs(hour_delta) >= 1.5:
            direction = "later" if hour_delta > 0 else "earlier"
            conf = _confidence(0.4 + min(abs(hour_delta) / 6, 0.4) + (0.1 if abs(hour_delta) >= 3 else 0))
            out.append(
                _finding(
                    "routine_shift_time",
                    "Routine Shift",
                    f"Posting clock moved {abs(hour_delta):.1f}h {direction}",
                    f"Mean posting hour: {hour_str(earlier['avg_hour'])} in the earlier period "
                    f"({earlier['posts']} posts) vs {hour_str(recent['avg_hour'])} in the last "
                    f"{m['recent_window_days']} days ({recent['posts']} posts). Night-share "
                    f"{earlier['night_share']:.0%} → {recent['night_share']:.0%}.",
                    "Posting time is a low-cost proxy for daily schedule. A sustained shift of "
                    "1.5h+ across the majority of recent posts is larger than normal day-to-day noise.",
                    f"This suggests a changed daily routine (different work shift, study period, "
                    f"travel or changed sleep pattern) rather than a single late night. It says "
                    f"nothing about mood on its own.",
                    conf,
                    earlier_hour=earlier["avg_hour"],
                    recent_hour=recent["avg_hour"],
                    delta=round(hour_delta, 2),
                    earlier_night=earlier["night_share"],
                    recent_night=recent["night_share"],
                )
            )

        # tone / vagueness shift
        sent_delta = recent["avg_sentiment"] - earlier["avg_sentiment"]
        vague_delta = recent["vague_rate"] - earlier["vague_rate"]
        solo_delta = recent["solo_rate"] - earlier["solo_rate"]
        if abs(sent_delta) >= 0.12 or vague_delta >= 0.15 or solo_delta >= 0.15:
            parts = []
            if abs(sent_delta) >= 0.12:
                parts.append(f"mean sentiment {earlier['avg_sentiment']:+.2f} → {recent['avg_sentiment']:+.2f}")
            if vague_delta >= 0.15:
                parts.append(f"vague-language rate {earlier['vague_rate']:.0%} → {recent['vague_rate']:.0%}")
            if solo_delta >= 0.15:
                parts.append(f"solo/independent language {earlier['solo_rate']:.0%} → {recent['solo_rate']:.0%}")
            parts.append(f"social-language rate {earlier['social_rate']:.0%} → {recent['social_rate']:.0%}")
            conf = _confidence(0.35 + 0.2 * (abs(sent_delta) >= 0.12) + 0.2 * (vague_delta >= 0.15) + 0.2 * (solo_delta >= 0.15))
            out.append(
                _finding(
                    "tone_shift",
                    "Routine Shift",
                    "Language became more inward-looking",
                    "; ".join(parts).capitalize() + ".",
                    "Lexicon frequency shifts are weak individually but meaningful when two or more "
                    "shift in the same direction at the same time as a scheduling change.",
                    "This may suggest a period of transition, withdrawal, or simply a change in what "
                    "the account chooses to publish. Public captions are curated, so this is a shift "
                    "in *presentation*, not necessarily in experience.",
                    conf,
                    sentiment_delta=round(sent_delta, 3),
                    vague_delta=round(vague_delta, 3),
                    solo_delta=round(solo_delta, 3),
                )
            )

        # interactant churn (relative share, not raw counts)
        earlier_total = sum(earlier["commenters"].values()) or 1
        recent_total = sum(recent["commenters"].values()) or 1
        faded, new = [], []
        for user in set(earlier["commenters"]) | set(recent["commenters"]):
            e_n = earlier["commenters"].get(user, 0)
            r_n = recent["commenters"].get(user, 0)
            e_s = e_n / earlier_total
            r_s = r_n / recent_total
            if e_n >= 3 and e_s >= 0.10 and r_s <= 0.4 * e_s:
                faded.append((user, e_n, r_n, e_s, r_s))
            elif r_n >= 3 and r_s >= 0.10 and e_s <= 0.4 * r_s:
                new.append((user, e_n, r_n, e_s, r_s))
        faded.sort(key=lambda x: -x[3])
        new.sort(key=lambda x: -x[4])

        if faded or new:
            bits = []
            if faded:
                bits.append(
                    "share fell: "
                    + ", ".join(f"{u} ({es:.0%} → {rs:.0%} of comments, {en}x → {rn}x)" for u, en, rn, es, rs in faded)
                )
            if new:
                bits.append(
                    "share rose: "
                    + ", ".join(f"{u} ({es:.0%} → {rs:.0%} of comments, {en}x → {rn}x)" for u, en, rn, es, rs in new)
                )
            conf = _confidence(0.4 + 0.15 * min(len(faded) + len(new), 3))
            out.append(
                _finding(
                    "network_churn",
                    "Anomaly",
                    "Interaction network rotated",
                    "; ".join(bits) + f". Window split at {m['recent_cutoff']}.",
                    "Comment share is a visible proxy for who the account interacts with. A contact "
                    "losing more than half their share while another gains is a rotation, not noise.",
                    "This suggests the active social circle shifted during the observation period. It "
                    "does not prove any relationship ended — close friends often go quiet in public "
                    "threads and move to DMs.",
                    conf,
                    faded=[{"user": u, "before": en, "after": rn} for u, en, rn, _, _ in faded],
                    new=[{"user": u, "before": en, "after": rn} for u, en, rn, _, _ in new],
                )
            )

    # 4. Anomalies -----------------------------------------------------------
    if earlier and recent:
        recent_night = m["recent"]["night_share"]
        earlier_night = m["earlier"]["night_share"]
        if recent_night - earlier_night >= 0.25 and m["recent"]["posts"] >= 5:
            out.append(
                _finding(
                    "late_night_spike",
                    "Anomaly",
                    "Late-night posting concentration",
                    f"{recent_night:.0%} of the last {m['recent_window_days']}-day posts fall between "
                    f"22:00–04:59, versus {earlier_night:.0%} earlier "
                    f"({m['recent']['posts']} vs {m['earlier']['posts']} posts).",
                    "Night posting is normally a small minority of an account's output. A jump of "
                    "25+ percentage points is well outside typical posting jitter.",
                    "This suggests reduced daytime availability or a deliberately quieter public "
                    "presence during the day. Insomnia, shift work, travel across time zones and "
                    "privacy-conscious posting all produce the same signal — the data cannot tell "
                    "them apart.",
                    _confidence(0.5 + min(recent_night - earlier_night, 0.4)),
                    earlier_night=earlier_night,
                    recent_night=recent_night,
                )
            )

        vol_delta = (recent["posts"] / max(m["recent_window_days"], 1)) - (
            earlier["posts"] / max(m["span"]["days"] - m["recent_window_days"], 1)
        )
        earlier_rate = earlier["posts"] / max(m["span"]["days"] - m["recent_window_days"], 1)
        meaningful = abs(vol_delta) >= 0.15 or (earlier_rate > 0 and abs(vol_delta) / earlier_rate >= 0.5)
        if abs(vol_delta) >= 0.05 and meaningful:
            out.append(
                _finding(
                    "volume_shift",
                    "Anomaly",
                    "Posting frequency changed",
                    f"{recent['posts'] / max(m['recent_window_days'], 1):.3f} posts/day recently vs "
                    f"{earlier['posts'] / max(m['span']['days'] - m['recent_window_days'], 1):.3f} posts/day earlier.",
                    "Output volume is the easiest signal to game and the easiest to under-read: people "
                    "post less when busy, when travelling, or when they choose a smaller audience.",
                    "This indicates a change in publishing habit, which may or may not reflect a change "
                    "in circumstances. It is the weakest of the signals listed here.",
                    "Low",
                    recent_per_day=round(recent["posts"] / max(m["recent_window_days"], 1), 3),
                    earlier_per_day=round(earlier["posts"] / max(m["span"]["days"] - m["recent_window_days"], 1), 3),
                )
            )

    if not out:
        out.append(
            _finding(
                "insufficient",
                "Method",
                "Not enough signal for a behavioural finding",
                f"{n} posts, {m['n_comments']} comments analysed.",
                "Findings require repeated patterns across time windows; single events are noise.",
                "No transparent possibility is offered because the data volume does not support one.",
                "Low",
            )
        )
    return out


def summarize(dataset: Dataset, target: str, recent_days: int = 90) -> dict:
    target = normalize_handle(target) if target else dataset.posts[0]["author"]
    metrics = build_metrics(dataset.posts, target, recent_days, extras=getattr(dataset, "extras", None))
    metrics["findings"] = detect_findings(metrics)
    metrics["data_quality"] = {
        "source": dataset.source_name,
        "warnings": dataset.warnings,
        "posts_with_location": sum(1 for p in dataset.posts if p["location"]),
        "posts_with_checkins": sum(1 for p in dataset.posts if p["checkins"]),
        "posts_with_comments": sum(1 for p in dataset.posts if p["comments"]),
        "unique_commenters": len({c["user"] for p in dataset.posts for c in p["comments"] if c["user"]}),
        "channels": metrics["channels"],
    }
    return metrics


def prompt_payload(m: dict) -> dict:
    """The compact, numbers-only slice we hand to the LLM."""
    keep = {
        "target": m["target"],
        "n_posts": m["n_posts"],
        "n_comments": m["n_comments"],
        "span": m["span"],
        "top_commenters": m["top_commenters"],
        "top_locations": m["top_locations"],
        "time_buckets": m["time_buckets"],
        "overall": m["overall"],
        "earlier_period": m["earlier"],
        "recent_period": m["recent"],
        "recent_window_days": m["recent_window_days"],
        "channels": m.get("channels"),
        "messages": m.get("messages"),
        "outgoing_comments": m.get("outgoing_comments"),
        "top_interactants": m.get("top_interactants", [])[:10],
        "known_contacts_count": len(m.get("known_contacts") or []),
        "co_location": {
            "total_episodes": m["co_location"]["total"],
            "by_user": m["co_location"]["episodes"],
            "details": m["co_location"]["details"][-8:],
        },
        "rule_based_findings": [
            {
                "title": f["title"],
                "data_fact": f["data_fact"],
                "confidence": f["confidence"],
            }
            for f in m["findings"]
        ],
    }
    return keep
