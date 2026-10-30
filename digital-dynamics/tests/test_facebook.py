"""
Facebook layer tests — importers, channel mapping, findings.

Offline: builds the synthetic export in a temp file, nothing touches the network.

    python -m pytest -q
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import facebook  # noqa: E402
import generate_mock_facebook_export as gen  # noqa: E402
import osint_core as core  # noqa: E402


@pytest.fixture(scope="module")
def zip_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("fb") / "export.zip"
    payload = gen.build(posts=50, seed=42)
    gen.write_zip(payload, str(path))
    return path


@pytest.fixture(scope="module")
def export(zip_path):
    return facebook.scan_export("export.zip", zip_path.read_bytes())


@pytest.fixture(scope="module")
def metrics(export):
    ds, info = facebook.build_dataset(export, include_messages=True, source_name="test")
    return core.summarize(ds, ds.posts[0]["author"], 90), ds, info


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_classification_recognises_every_group(export):
    kinds = {f["kind"] for f in export.files}
    assert {"posts", "messages", "comments", "places", "friends", "other"} <= kinds
    posts_file = next(f for f in export.files if f["kind"] == "posts")
    assert posts_file["path"].endswith("your_posts__check_ins__photos_and_videos_1.json")


def test_likes_and_reactions_ignored_not_guessed(export):
    likes = [f for f in export.files if "likes_and_reactions" in f["path"]]
    assert likes and likes[0]["kind"] == "other"
    assert not export.errors


def test_owner_inferred_from_messenger(export):
    assert export.owner == gen.OWNER
    assert all(e["user"] != "@Nadia_Rahman" for e in export.message_events)


def test_single_json_upload_still_works(tmp_path):
    payload = gen.build(posts=12, seed=1)
    path = tmp_path / "posts.json"
    path.write_text(json.dumps(payload["posts"]))
    exp = facebook.scan_export("posts.json", path.read_bytes())
    assert len(exp.posts) == 12
    assert "messages" not in {f["kind"] for f in exp.files}
    assert not [n for n in exp.notes if "No posts file" in n]
    ds, _ = facebook.build_dataset(exp, include_messages=True)
    assert any("Messenger threads found" in w for w in ds.warnings)  # honest gap, not silence


def test_bad_zip_and_bad_json_are_reported():
    assert facebook.scan_export("x.zip", b"not a zip").errors
    assert facebook.scan_export("x.json", b"{nope").errors


# --------------------------------------------------------------------------- #
# Mapping to the app schema
# --------------------------------------------------------------------------- #
def test_channels_present(metrics):
    m, ds, info = metrics
    ch = m["channels"]
    assert ch["posts"] == 50
    assert ch["messages"] > 500
    assert ch["comments_given"] == 40
    assert ch["comments_received"] == 0        # Facebook exports have no timeline replies
    assert ch["location_events"] == 50
    assert info["owner_detected"] == gen.OWNER


def test_messages_can_be_excluded(export):
    ds, _ = facebook.build_dataset(export, include_messages=False)
    assert ds.extras["message_events"] == []
    assert any("excluded" in w for w in ds.warnings)
    m = core.summarize(ds, ds.posts[0]["author"], 90)
    assert m["channels"]["messages"] == 0
    assert not [f for f in m["findings"] if f["key"].startswith("message")]


def test_tagged_post_becomes_shared_presence(metrics):
    m, _, _ = metrics
    cluster = m["co_location"]["clusters"][0]
    assert cluster["user"] == "@Aria_Chen"
    assert cluster["venue"] == "Blue Door Cafe"
    assert cluster["count"] == 4
    assert cluster["basis"] == "tagged_post"


def test_top_interactants_split_by_channel(metrics):
    m, _, _ = metrics
    top = m["top_interactants"][0]
    assert top["account"] == "@Maya_Lin"
    assert top["messages"] > top["comments_out"]


def test_interactant_handles_are_normalised(metrics):
    m, _, _ = metrics
    assert all(r["account"].startswith("@") for r in m["top_interactants"])
    assert " " not in "".join(r["account"] for r in m["top_interactants"])


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
def test_expected_findings_appear(metrics):
    m, _, _ = metrics
    keys = {f["key"] for f in m["findings"]}
    assert "message_network" in keys
    assert "message_shift" in keys
    assert "co_location" in keys
    assert "non_friend_contact" in keys


def test_message_rotation_direction(metrics):
    m, _, _ = metrics
    f = next(f for f in m["findings"] if f["key"] == "message_shift")
    rising = {r["user"] for r in f["evidence"]["rising"]}
    falling = {r["user"] for r in f["evidence"]["falling"]}
    assert "@Aria_Chen" in rising
    assert "@Jonas_Weber" in falling
    for r in f["evidence"]["rising"]:
        assert r["recent"] > r["earlier"]


def test_private_message_text_never_reaches_the_prompt(metrics):
    m, _, _ = metrics
    blob = json.dumps(core.prompt_payload(m), default=str).lower()
    for content in gen.MESSAGES["Aria Chen"] + gen.MESSAGES["Maya Lin"]:
        assert content.lower() not in blob, "message bodies must never be sent to the LLM"


def test_offline_report_mentions_channels(metrics):
    import ai_analyst

    m, _, _ = metrics
    report = ai_analyst.offline_report(core.prompt_payload(m))
    assert "# Transparent Possibility Report" in report
    assert "Channels:" in report
    assert "## Limitations & Disclaimer" in report


def test_every_finding_has_the_four_parts(metrics):
    m, _, _ = metrics
    for f in m["findings"]:
        assert f["data_fact"] and f["logical_reasoning"] and f["possibility"]
        assert f["confidence"] in {"Low", "Medium", "High"}
        assert f["category"] in {"Core Network", "Location Overlap", "Routine Shift", "Anomaly", "Method"}


# --------------------------------------------------------------------------- #
# Graph API mapping (no network)
# --------------------------------------------------------------------------- #
def test_graph_mapping():
    feed = [
        {"id": "1_1", "message": "Open day at the studio", "created_time": "2026-09-01T10:00:00+0000",
         "place": {"name": "Studio"}, "permalink_url": "https://facebook.com/1_1"},
        {"id": "1_2", "story": "Page updated its hours", "created_time": "2026-09-03T09:00:00+0000"},
        {"id": "1_3", "created_time": "2026-09-04T09:00:00+0000"},  # no text -> dropped
    ]
    comments = {"1_1": [{"message": "coming!", "created_time": "2026-09-01T11:00:00+0000",
                         "from": {"name": "Maya Lin"}}]}
    posts = facebook.graph_to_posts(feed, comments, page_handle="@mypage")
    assert len(posts) == 2
    assert posts[0]["location"] == "Studio"
    assert posts[0]["comments"][0]["user"] == "@Maya_Lin"
    assert posts[0]["checkins"][0]["location"] == "Studio"
    assert posts[1]["location"] is None


def test_graph_requires_token():
    posts, warnings, errors, meta = facebook.graph_fetch("", "", limit=1)
    assert errors or not posts
