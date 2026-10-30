"""
Smoke tests for the analysis layer. No Streamlit, no network, no API key.

    python -m pytest -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import osint_core as core  # noqa: E402
import ai_analyst  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def metrics():
    ds = core.load_upload("mock_data.json", (ROOT / "mock_data.json").read_bytes())
    assert ds.ok
    return core.summarize(ds, "@nadia_wanders", 90)


def test_load_json_and_csv(metrics):
    assert metrics["n_posts"] == 50
    csv_ds = core.load_upload("sample_data.csv", (ROOT / "sample_data.csv").read_bytes())
    assert csv_ds.ok and len(csv_ds.posts) == 50
    assert len(core.summarize(csv_ds, "@nadia_wanders", 90)["findings"]) >= 4


def test_bad_input_is_reported_not_crashed():
    assert core.load_upload("x.json", b'{"nope": 1}').errors
    assert core.load_upload("x.txt", b"abc").errors
    assert core.load_upload("x.json", b"not json").errors


def test_top_interactant_and_shares(metrics):
    user, count = metrics["top_commenters"][0]
    assert user == "@best_friend"
    assert count / metrics["n_comments"] > 0.2


def test_co_location_cluster_found(metrics):
    cluster = metrics["co_location"]["clusters"][0]
    assert cluster["user"] == "@romantic_interest"
    assert cluster["venue"] == "Blue Door Cafe"
    assert cluster["count"] >= 3
    assert cluster["spread_days"] <= 21


def test_routine_shift_direction(metrics):
    assert metrics["recent"]["avg_hour"] > metrics["earlier"]["avg_hour"] + 1.5
    assert metrics["recent"]["night_share"] > metrics["earlier"]["night_share"]


def test_every_finding_has_the_four_parts(metrics):
    for f in metrics["findings"]:
        assert f["data_fact"] and f["logical_reasoning"] and f["possibility"]
        assert f["confidence"] in {"Low", "Medium", "High"}
        assert f["disclaimer"]
        assert f["category"] in {"Core Network", "Location Overlap", "Routine Shift", "Anomaly", "Method"}


def test_offline_report_structure(metrics):
    report = ai_analyst.offline_report(core.prompt_payload(metrics))
    for heading in ("# Transparent Possibility Report", "## 1. Core Network Analysis",
                    "## 4. Anomaly Detection", "## Limitations & Disclaimer"):
        assert heading in report
    # no invented certainty in the deterministic engine
    assert " definitely " not in report.lower()


def test_prompt_payload_has_no_raw_text_blob(metrics):
    payload = core.prompt_payload(metrics)
    assert "raw_posts" not in payload
    json.dumps(payload)  # must be serialisable


def test_missing_key_resolves_to_none(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert ai_analyst.resolve_api_key(None) is None
    assert ai_analyst.resolve_api_key("  gsk_test  ") == "gsk_test"
