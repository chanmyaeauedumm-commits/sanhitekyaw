"""
Tests for the data-acquisition layer: platform export importers and the
Mastodon / Reddit mapping functions.

All offline — fixtures only. The one live network test is opt-in:
    LIVE=1 python -m pytest tests/test_connectors.py -q
"""

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))          # archived modules
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # osint_core stays at the root

import connectors  # noqa: E402
import importers  # noqa: E402
import osint_core as core  # noqa: E402

FX = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- #
# Importers
# --------------------------------------------------------------------------- #
def test_detect_formats():
    assert importers.detect_format("tweets.js", (FX / "tweets.js").read_bytes()) == "x_archive"
    assert importers.detect_format("posts_1.json", (FX / "instagram_posts_1.json").read_bytes()) == "instagram"
    assert importers.detect_format("fb.json", (FX / "facebook_posts_1.json").read_bytes()) == "facebook"
    assert importers.detect_format("data.csv", b"a,b\n1,2\n") == "csv"
    assert importers.detect_format("x.zip", b"PK\x03\x04") == "zip"


def test_x_archive_js():
    ds, info = importers.import_export("tweets.js", (FX / "tweets.js").read_bytes())
    assert ds.ok and info["format"] == "x_archive"
    assert len(ds.posts) == 3
    assert ds.posts[0]["tags"] == ["@friend_a", "@friend_b", "#weekend"]
    assert ds.posts[0]["timestamp"].year == 2025
    assert any("no reply threads" in w for w in ds.warnings)


def test_x_archive_zip_username_and_no_email_leak():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data/tweets.js", (FX / "tweets.js").read_text())
        zf.writestr("data/account.js", (FX / "account.js").read_text())
    ds, info = importers.import_export("twitter.zip", buf.getvalue())
    assert ds.ok
    assert ds.posts[0]["author"] == "@target_handle"
    blob = json.dumps([{k: v for k, v in p.items() if k != "timestamp"} for p in ds.posts], default=str)
    assert "private@example.com" not in blob, "importer must never surface the archive email"


def test_instagram_json_comments_grouped():
    ds, info = importers.import_export("posts_1.json", (FX / "instagram_posts_1.json").read_bytes())
    assert ds.ok and info["format"] == "instagram"
    assert len(ds.posts) == 3
    assert len(ds.posts[0]["comments"]) == 2
    assert ds.posts[0]["comments"][0]["user"].startswith("@")
    assert any("unknown_commenter" in w for w in ds.warnings)


def test_instagram_mentions_become_tags():
    items = [{"media": [{"creation_timestamp": 1750000000, "title": "with @friend_a and @friend_b today"}],
              "creation_timestamp": 1750000000, "comments": []}]
    posts, _ = importers.instagram_to_posts(items)
    assert posts[0]["tags"] == ["@friend_a", "@friend_b"]


def test_facebook_export_place_and_skips():
    ds, info = importers.import_export("fb.json", (FX / "facebook_posts_1.json").read_bytes())
    assert ds.ok and info["format"] == "facebook"
    assert len(ds.posts) == 2  # two rows are intentionally unusable
    assert any(p["location"] == "East Coast Park" for p in ds.posts)


def test_zip_without_known_file_errors_cleanly():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("media/x.jpg", b"\x00")
    ds, info = importers.import_export("random.zip", buf.getvalue())
    assert ds is not None and ds.errors and not ds.posts


def test_csv_still_falls_through_to_core():
    ds, info = importers.import_export(
        "sample_data.csv", (Path(__file__).resolve().parents[1] / "sample_data.csv").read_bytes()
    )
    assert ds.ok and len(ds.posts) == 50 and info["format"] == "csv"


# --------------------------------------------------------------------------- #
# Mastodon mapping
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def masto():
    return json.loads((FX / "mastodon.json").read_text())


def test_mastodon_mapping(masto):
    res = connectors.mastodon_to_posts(masto["account"], masto["statuses"], masto["contexts"])
    assert not res.errors
    assert res.meta["account"] == "@target_handle"
    # the empty-content status is dropped, the boost is kept
    texts = [p["post_text"] for p in res.posts]
    assert len(texts) == 3
    assert "Original by someone else" in texts[-1]
    # HTML stripped, entities unescaped
    assert texts[0] == "Long walk at the park. & tea."
    # replies become comments with handles and timestamps
    first = res.posts[0]
    assert {c["user"] for c in first["comments"]} == {"@friend_a", "@stranger"}
    assert first["comments"][0]["timestamp"].startswith("2026-09-01")
    # mentions become tags; no fabricated locations
    assert first["tags"] == ["@friend_a"]
    assert all(p["location"] is None for p in res.posts)
    assert any("no venue" in w.lower() for w in res.warnings)


def test_mastodon_mapping_feeds_the_analyzer(masto):
    res = connectors.mastodon_to_posts(masto["account"], masto["statuses"], masto["contexts"])
    ds = core.normalize_posts(res.posts, "@target_handle")
    metrics = core.summarize(ds, "@target_handle", 30)
    assert metrics["n_posts"] == 3
    assert metrics["n_comments"] == 2
    assert metrics["top_commenters"][0][0] in {"@friend_a", "@stranger"}
    assert metrics["top_locations"] == []  # no invented venues


def test_mastodon_errors_are_readable(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(connectors, "_get_json", boom)
    res = connectors.mastodon_fetch("mastodon.social", "@nobody")
    assert res.errors and "network unreachable" in res.errors[0].lower()


# --------------------------------------------------------------------------- #
# Reddit mapping
# --------------------------------------------------------------------------- #
def test_reddit_mapping_filters_deleted_and_foreign():
    data = json.loads((FX / "reddit.json").read_text())
    res = connectors.reddit_to_posts("target_handle", data["submissions"], data["comments"])
    assert not res.errors
    assert len(res.posts) == 2  # p3 belongs to another user -> dropped
    assert all(p["author"] == "@target_handle" for p in res.posts)
    # [deleted] comment dropped
    assert [c["user"] for c in res.posts[0]["comments"]] == ["@helper_one"]
    # subreddit is kept as context but never faked into `location`
    assert res.posts[0]["location"] is None
    assert res.posts[0]["subreddit"] == "test"


def test_reddit_requires_credentials():
    res = connectors.reddit_fetch("someone", "", "")
    assert res.errors


# --------------------------------------------------------------------------- #
# Opt-in live check
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 to hit the public Mastodon API")
def test_live_mastodon():
    res = connectors.mastodon_fetch("mastodon.social", "@MastodonEngineering", limit=5, max_threads=2, delay=0.2)
    assert not res.errors and res.posts
    assert res.meta["platform"] == "mastodon"
