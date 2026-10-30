"""
generate_mock_data.py
=====================
Creates realistic *fictional* social-media data in the shape the app expects,
so you can demo / test the analyzer without touching a real person's profile.

Every username, comment and location below is invented. Nothing here is scraped
from a real platform and nothing here describes a real person.

Embedded patterns (so the analyzer has something to find):
  * @best_friend            -> comments on ~80% of all posts, warm / inside-joke tone
  * @romantic_interest      -> barely present early, appears often in the last 90 days
  * co-location             -> 4 shared cafe check-ins with @romantic_interest in 14 days
  * routine shift           -> posting hour drifts from ~20:00 to ~23:00 recently
  * tone shift              -> posts get shorter, vaguer, more "independent"/solo
  * contact churn           -> @coworker_jin is a top interactant early, nearly gone late

Usage:
    python generate_mock_data.py                  # writes mock_data.json
    python generate_mock_data.py --posts 120 --seed 7 --out big.json
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta

TARGET = "@nadia_wanders"

FRIENDS = [
    "@best_friend",
    "@coworker_jin",
    "@gym_buddy_alex",
    "@hiking_crew",
    "@cousin_may",
    "@old_colleague_sam",
    "@romantic_interest",
    "@neighbour_tom",
]

CAFES = ["Kopi & Co.", "Blue Door Cafe", "Riverside Roasters", "The Reading Room"]
GYMS = ["Iron Yard Gym", "Sunrise Fitness"]
PARKS = ["Bishan Park", "Botanic Gardens", "East Coast Trail"]
FOOD = ["Hawker Row", "Noodle House 88", "Tandoor Lane", "Sushi Nine"]

# --- post captions -----------------------------------------------------------
EARLY_CAPTIONS = [
    "Coffee run before standup. {loc} has the best flat white in the city.",
    "Squat day at {loc}. Legs are officially gone. Worth it.",
    "Sunday walk through {loc} with the camera. Golden hour was unreal.",
    "Dinner at {loc} with the usual suspects. Laughing until my ribs hurt.",
    "Finally finished that book at {loc}. Ask me about the ending (please don't).",
    "Long week. {loc}, a flat white and zero notifications for an hour.",
    "Trail run at {loc} this morning. Saw three otters. Three!",
    "Team lunch at {loc}. Someone ordered for the whole table again.",
    "Two hours of badminton then noodles at {loc}. Peak Saturday.",
    "Rainy day = {loc} and a very slow crossword.",
]

LATE_CAPTIONS = [
    "Up late. Thinking about things. Not posting about them.",
    "Some rooms you only understand at 11pm. Mine is one of them.",
    "Quiet week. Went where I wanted, alone, and it was good.",
    "New routine. Different timezone of the day, same person.",
    "Coffee at {loc}. Small table, big notebook. Working on something.",
    "Not everything needs an explanation. Or a caption.",
    "Late walk. Streetlights and no plans.",
    "Stepped out of the usual loop. Feels lighter.",
    "Reread old messages tonight. Filed them away.",
    "You can like the people around you and still want a quieter room.",
]

BEST_FRIEND_COMMENTS = [
    "inside joke #14, you know exactly what I mean",
    "okay but you still owe me that flat white",
    "sending you a meme about this in 3, 2, 1",
    "proud of you, obviously",
    "we said NO more gym before 7am",
    "I'm reading this as a cry for help and also dinner",
    "remember when we got lost looking for this place",
    "your playlist for this was objectively terrible",
]

COWORKER_JIN_COMMENTS = [
    "reviewing that doc before 10, promise",
    "standup moved to 9:15 btw",
    "I'll take a flat white too, thanks",
    "same. adding it to the team retro",
]

GENERIC_COMMENTS = [
    "this place is great",
    "need to try it",
    "love this for you",
    "absolutely stunning shot",
    "count me in next time",
    "hahaha this is so you",
]

ROMANTIC_COMMENTS = [
    "you picked a good table today",
    "next one's on me",
    "we should make this a weekly thing",
    "that view never gets old, does it",
    "I was there an hour after you, funny timing",
]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def generate(posts: int = 50, seed: int = 42, end: datetime | None = None) -> list[dict]:
    rng = random.Random(seed)
    end = (end or datetime.now()).replace(microsecond=0)
    start = end - timedelta(days=365)

    # ---- choose post days -------------------------------------------------
    n_recent = max(12, posts // 3)              # posts inside the last 90 days
    overlap_days = sorted(rng.sample(range(0, 14), 4))   # 4 cafe co-location days
    rest_recent = rng.sample([d for d in range(0, 90) if d not in overlap_days], n_recent - 4)
    older = rng.sample(range(90, 365), posts - n_recent)
    days = sorted(overlap_days + rest_recent + older)    # oldest -> newest (by days ago)

    records: list[dict] = []
    overlap_set = set(overlap_days)

    for i, days_ago in enumerate(days):
        post_day = (end - timedelta(days=days_ago)).date()
        recent = days_ago <= 90
        if recent:
            hour = rng.choice([21, 22, 22, 23, 23, 23, 0])
        else:
            hour = rng.choice([7, 8, 12, 13, 17, 18, 19, 19, 20, 20, 20, 21])

        ts = datetime.combine(post_day, datetime.min.time()) + timedelta(
            hours=hour, minutes=rng.randint(0, 59)
        )
        ts = min(ts, end)

        if days_ago in overlap_set:
            location = "Blue Door Cafe"
        else:
            location = rng.choice(rng.choice([CAFES, GYMS, PARKS, FOOD]))

        caption = rng.choice(LATE_CAPTIONS if recent else EARLY_CAPTIONS)
        text = caption.format(loc=location) if "{loc}" in caption else caption

        comments: list[dict] = []
        # @best_friend on ~80% of posts
        if rng.random() < 0.80:
            comments.append(_comment("@best_friend", rng.choice(BEST_FRIEND_COMMENTS), ts, rng))

        # @coworker_jin heavy early, fades late
        jin_p = 0.10 if recent else 0.55
        if rng.random() < jin_p:
            comments.append(_comment("@coworker_jin", rng.choice(COWORKER_JIN_COMMENTS), ts, rng))

        # @romantic_interest almost absent early, very present late
        ri_p = 0.70 if recent else 0.05
        if rng.random() < ri_p:
            comments.append(_comment("@romantic_interest", rng.choice(ROMANTIC_COMMENTS), ts, rng))

        for _ in range(rng.randint(0, 3)):
            friend = rng.choice([f for f in FRIENDS if f not in {"@best_friend", "@coworker_jin", "@romantic_interest"}])
            comments.append(_comment(friend, rng.choice(GENERIC_COMMENTS), ts, rng))

        comments = comments[:10]
        comments.sort(key=lambda c: c["timestamp"])

        tags = rng.sample(
            [f for f in FRIENDS], k=rng.randint(1, 3)
        )

        # Everybody's check-ins live on the same post object.
        checkins = [{"user": TARGET, "location": location, "timestamp": _iso(ts)}]
        if days_ago in overlap_set:
            checkins.append(
                {
                    "user": "@romantic_interest",
                    "location": location,
                    "timestamp": _iso(ts + timedelta(minutes=rng.randint(25, 95))),
                }
            )
        elif rng.random() < 0.25:
            other = rng.choice(FRIENDS)
            checkins.append(
                {
                    "user": other,
                    "location": location,
                    "timestamp": _iso(ts + timedelta(minutes=rng.randint(15, 120))),
                }
            )

        records.append(
            {
                "id": f"post_{i + 1:03d}",
                "author": TARGET,
                "post_text": text,
                "timestamp": _iso(ts),
                "location": location,
                "comments": comments,
                "tags": tags,
                "checkins": checkins,
                "source": "synthetic-demo",
            }
        )

    records.sort(key=lambda r: r["timestamp"])
    return records


def _comment(user: str, text: str, post_ts: datetime, rng: random.Random) -> dict:
    return {
        "user": user,
        "text": text,
        "timestamp": _iso(post_ts + timedelta(minutes=rng.randint(3, 600))),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate fictional OSINT demo data.")
    ap.add_argument("--posts", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="mock_data.json")
    args = ap.parse_args()

    data = generate(posts=args.posts, seed=args.seed)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    print(f"Wrote {len(data)} fictional posts -> {args.out}")
    print("Reminder: this data is synthetic. Do not mix it with real personal data.")


if __name__ == "__main__":
    main()
