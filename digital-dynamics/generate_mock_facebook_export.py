"""
generate_mock_facebook_export.py
================================
Builds a **fake** Facebook "Download your information" archive that mirrors Meta's
real folder layout, so the whole Facebook pipeline can be demoed and tested
without touching anyone's real data.

Every name, message and venue is invented. No real person is described.

Output (default `facebook_demo_export.zip`):

    facebook-<name>-<date>/
      your_activity_across_facebook/
        posts/your_posts__check_ins__photos_and_videos_1.json
        comments/comments.json
        places/places_visited.json
      friends_and_followers/friends.json
      your_messages/inbox/<thread>/message_1.json

Patterns embedded for the analyzer to find
-----------------------------------------
1. Posts drift from ~20:00 to ~23:00 in the last 90 days.
2. Language turns inward: fewer "we/us", more "alone / quiet / not posting".
3. Four posts in the last 14 days tag Aria Chen at Blue Door Cafe
   -> repeated tagged-post co-location.
4. Messenger: Maya Lin is the constant thread; Aria Chen's thread barely exists
   early and dominates the last 3 months (and goes late-night).
5. Jonas Weber's thread fades away in the last quarter.
6. Aria Chen is deliberately **absent from friends.json**, which should raise the
   "busiest interactant is not in the friend list" flag.

Usage:
    python generate_mock_facebook_export.py
    python generate_mock_facebook_export.py --posts 120 --seed 7 --out big.zip
"""

from __future__ import annotations

import argparse
import json
import random
import zipfile
from datetime import datetime, timedelta

OWNER = "Nadia Rahman"
TARGET = "@Nadia_Rahman"

# name -> (thread title, early message probability, recent message probability)
CONTACTS = {
    "Maya Lin": (0.75, 0.70),          # best friend, constant
    "Jonas Weber": (0.45, 0.05),       # coworker, fades out
    "Priya Raman": (0.20, 0.18),       # cousin, steady
    "Tomas Alvarez": (0.15, 0.12),     # gym friend, steady
    "Kenji Sato": (0.10, 0.08),        # old colleague, occasional
    "Aria Chen": (0.02, 0.80),         # barely present early, dominant late
}

FRIENDS = ["Maya Lin", "Jonas Weber", "Priya Raman", "Tomas Alvarez", "Kenji Sato"]  # Aria Chen absent on purpose

VENUES = {
    "cafe": ["Blue Door Cafe", "Kopi & Co.", "Riverside Roasters"],
    "gym": ["Iron Yard Gym", "Sunrise Fitness"],
    "park": ["Bishan Park", "Botanic Gardens"],
    "food": ["Hawker Row", "Noodle House 88", "Tandoor Lane"],
}

EARLY_POSTS = [
    "Coffee run before standup. {venue} still has the best flat white.",
    "Squat day at {venue}. Legs are officially gone.",
    "Sunday walk through {venue} with the camera. Golden hour was unreal.",
    "Dinner at {venue} with the usual suspects. Laughing until my ribs hurt.",
    "Long week. {venue}, a flat white and zero notifications for an hour.",
    "Two hours of badminton then noodles at {venue}. Peak Saturday.",
    "Rainy day = {venue} and a very slow crossword.",
    "Team lunch at {venue}. Someone ordered for the whole table again.",
]

LATE_POSTS = [
    "Up late. Thinking about things. Not posting about them.",
    "Quiet week. Went where I wanted, alone, and it was good.",
    "New routine. Different timezone of the day, same person.",
    "Coffee at {venue}. Small table, big notebook. Working on something.",
    "Not everything needs an explanation. Or a caption.",
    "Late walk. Streetlights and no plans.",
    "Stepped out of the usual loop. Feels lighter.",
    "You can like the people around you and still want a quieter room.",
]

MESSAGES = {
    "Maya Lin": ["inside joke #14, you know what I mean", "flat white tomorrow?", "sending you a meme",
                 "proud of you, obviously", "ok but sleep at some point", "call me when you're free"],
    "Jonas Weber": ["standup moved to 9:15", "reviewing the doc now", "can you check the deck before 4?",
                    "retro notes attached", "thanks for covering yesterday"],
    "Priya Raman": ["amma asked about sunday lunch", "bring the good camera", "we're doing 1pm at grandma's"],
    "Tomas Alvarez": ["leg day skipped again?", "gym at 7?", "you free thursday"],
    "Kenji Sato": ["long time! how's the new place", "sending you that article", "we should catch up properly"],
    "Aria Chen": ["you picked a good table today", "next one's on me", "the view never gets old",
                  "I was there an hour after you, funny timing", "still up?", "this is becoming a habit",
                  "quiet one tonight", "walked past the cafe and thought of you"],
}

OUTGOING_COMMENTS = [
    "this place is great, need to go back",
    "love this for you",
    "count me in next time",
    "hahaha this is so you",
    "we should make this a weekly thing",
]


def _thread(name: str, messages: list[dict]) -> dict:
    return {
        "participants": [{"name": OWNER}, {"name": name}],
        "messages": sorted(messages, key=lambda m: m["timestamp_ms"]),
        "title": name,
        "is_still_participant": True,
        "folder": "inbox",
        "thread_type": "Regular",
        "thread_path": f"inbox/{name.lower().replace(' ', '_')}",
    }


def _msg(name: str, dt: datetime, content: str) -> dict:
    return {
        "sender_name": name,
        "timestamp_ms": int(dt.timestamp() * 1000),
        "content": content,
        "type": "Generic",
        "is_unsent": False,
    }


def build(posts: int = 50, seed: int = 42, end: datetime | None = None) -> dict[str, object]:
    rng = random.Random(seed)
    end = (end or datetime.now()).replace(microsecond=0)
    start = end - timedelta(days=365)

    n_recent = max(12, posts // 3)
    overlap_days = sorted(rng.sample(range(0, 14), 4))
    rest_recent = rng.sample([d for d in range(0, 90) if d not in overlap_days], n_recent - 4)
    older = rng.sample(range(90, 365), posts - n_recent)
    days = sorted(overlap_days + rest_recent + older)
    overlap_set = set(overlap_days)

    posts_payload: list[dict] = []
    places_payload: list[dict] = []
    comments_payload: list[dict] = []
    threads: dict[str, list[dict]] = {name: [] for name in CONTACTS}

    for days_ago in days:
        day = (end - timedelta(days=days_ago)).date()
        recent = days_ago <= 90
        hour = rng.choice([21, 22, 22, 23, 23, 23, 0]) if recent else rng.choice([7, 8, 12, 13, 17, 18, 19, 19, 20, 20, 20, 21])
        ts = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=rng.randint(0, 59))
        ts = min(ts, end)
        epoch = int(ts.timestamp())

        if days_ago in overlap_set:
            venue, tags = "Blue Door Cafe", ["Aria Chen"]
        else:
            pool = rng.choice(list(VENUES.values()))
            venue, tags = rng.choice(pool), rng.sample(FRIENDS, k=rng.randint(0, 2))

        caption = rng.choice(LATE_POSTS if recent else EARLY_POSTS)
        text = caption.format(venue=venue) if "{venue}" in caption else caption

        entry = {"timestamp": epoch, "title": text, "place": venue}
        if tags:
            entry["tags"] = [{"name": t} for t in tags]
        posts_payload.append(entry)
        places_payload.append(
            {"name": venue, "timestamp": epoch, "coordinate": {"latitude": 1.35, "longitude": 103.82}}
        )

    # ---- Messenger --------------------------------------------------------
    for name, (early_p, late_p) in CONTACTS.items():
        for days_ago in range(365, -1, -1):
            day = end - timedelta(days=days_ago)
            recent = days_ago <= 90
            p = late_p if recent else early_p
            # romantic thread ramps up: only the last ~110 days are dense
            if name == "Aria Chen" and days_ago > 110:
                p = 0.01
            if rng.random() > p:
                continue
            for _ in range(rng.randint(1, 3)):
                if recent and name == "Aria Chen":
                    hour = rng.choice([21, 22, 23, 23, 0])
                else:
                    hour = rng.choice([8, 9, 12, 13, 18, 19, 20, 21])
                dt = day.replace(hour=hour, minute=rng.randint(0, 59), second=0, microsecond=0)
                if dt > end:
                    dt = end - timedelta(minutes=rng.randint(1, 90))
                threads[name].append(_msg(name, dt, rng.choice(MESSAGES[name])))
                if rng.random() < 0.5:  # the owner replies
                    threads[name].append(
                        _msg(OWNER, dt + timedelta(minutes=rng.randint(1, 40)), rng.choice(["ha", "ok", "on my way", "later tonight", "sure"]))
                    )

    # ---- comments the owner left on other people's posts ------------------
    for days_ago in rng.sample(range(0, 365), 40):
        day = (end - timedelta(days=days_ago)).date()
        person = rng.choice(FRIENDS + ["Aria Chen"])
        ts = datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(9, 23), minutes=rng.randint(0, 59))
        comments_payload.append(
            {
                "timestamp": int(min(ts, end).timestamp()),
                "title": f"You commented on {person}'s post.",
                "data": [{"comment": {}}],
            }
        )

    return {
        "posts": posts_payload,
        "places": places_payload,
        "comments": comments_payload,
        "friends": [{"name": n} for n in FRIENDS],
        "threads": {name: _thread(name, msgs) for name, msgs in threads.items() if msgs},
    }


def write_zip(payload: dict[str, object], path: str, end: datetime | None = None) -> None:
    end = end or datetime.now()
    root = f"facebook-nadiarahman-{end.strftime('%Y-%m-%d')}"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{root}/your_activity_across_facebook/posts/your_posts__check_ins__photos_and_videos_1.json",
            json.dumps(payload["posts"], indent=2),
        )
        zf.writestr(
            f"{root}/your_activity_across_facebook/comments/comments.json",
            json.dumps(payload["comments"], indent=2),
        )
        zf.writestr(
            f"{root}/your_activity_across_facebook/places/places_visited.json",
            json.dumps(payload["places"], indent=2),
        )
        zf.writestr(
            f"{root}/friends_and_followers/friends.json",
            json.dumps(payload["friends"], indent=2),
        )
        for name, thread in (payload["threads"] or {}).items():
            folder = name.lower().replace(" ", "_")
            zf.writestr(
                f"{root}/your_messages/inbox/{folder}/message_1.json",
                json.dumps(thread, indent=2),
            )
        # Deliberately include a file the scanner should report as ignored,
        # so 'what was recognised' is visible in the UI.
        zf.writestr(
            f"{root}/your_activity_across_facebook/likes_and_reactions/likes_and_reactions_1.json",
            json.dumps([{"timestamp": 1735700000, "data": [{"reaction": {"reaction": "LIKE", "actor": OWNER}}]}], indent=2),
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a fake Facebook data export for testing.")
    ap.add_argument("--posts", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="facebook_demo_export.zip")
    args = ap.parse_args()

    payload = build(posts=args.posts, seed=args.seed)
    write_zip(payload, args.out)
    print(f"Wrote {args.out}")
    print(f"  posts: {len(payload['posts'])} · places: {len(payload['places'])} · "
          f"comments: {len(payload['comments'])} · friends: {len(payload['friends'])} · "
          f"message threads: {len(payload['threads'])}")
    print("Reminder: this archive is synthetic. Do not mix it up with a real export.")


if __name__ == "__main__":
    main()
