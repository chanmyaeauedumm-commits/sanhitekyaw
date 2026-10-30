"""
Pasted-link parsing and owner cross-check. Pure functions, no network.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import facebook_links as fl  # noqa: E402


def test_vanity_profile():
    r = fl.parse_facebook_url("https://www.facebook.com/zuck")
    assert r["kind"] == "profile" and r["username"] == "zuck"
    assert r["resolvable"] and r["canonical"] == "https://www.facebook.com/zuck"


def test_mobile_host_and_tracking_stripped():
    r = fl.parse_facebook_url("https://web.facebook.com/nadia.rahman/?_rdc=1&_rdr&mibextid=abc")
    assert r["username"] == "nadia.rahman"
    assert r["canonical"] == "https://www.facebook.com/nadia.rahman"
    assert any("tracking" in n for n in r["notes"])


def test_profile_php_id():
    r = fl.parse_facebook_url("https://www.facebook.com/profile.php?id=100012345678")
    assert r["kind"] == "profile" and r["numeric_id"] == "100012345678"
    assert r["canonical"] == "https://www.facebook.com/profile.php?id=100012345678"


def test_page_link_keeps_id():
    r = fl.parse_facebook_url("https://www.facebook.com/pages/Blue-Door-Cafe/123456789")
    assert r["kind"] == "page" and r["numeric_id"] == "123456789"
    assert r["canonical"] == "https://www.facebook.com/123456789"
    assert any("Graph API" in n for n in r["notes"])


def test_post_links_are_marked_as_content_not_accounts():
    for url, post_id in [
        ("https://www.facebook.com/permalink.php?story_fbid=111&id=222", "111"),
        ("https://m.facebook.com/story.php?story_fbid=9&id=8", "9"),
        ("https://www.facebook.com/photo?fbid=555&set=a.666", "555"),
        ("https://www.facebook.com/watch/?v=999", "999"),
        ("https://www.facebook.com/zuck/posts/pfbid02abc", "pfbid02abc"),
    ]:
        r = fl.parse_facebook_url(url)
        assert r["kind"] == "post", url
        assert r["post_id"] == post_id, url
        assert "story_fbid=" in (r["canonical"] or "") or "fbid=" in (r["canonical"] or "") \
            or "/posts/" in (r["canonical"] or "") or "v=" in (r["canonical"] or ""), url


def test_short_links_are_flagged_not_guessed():
    r = fl.parse_facebook_url("https://www.facebook.com/share/p/AbC123/")
    assert r["kind"] == "short_link" and not r["resolvable"]
    assert "Open the link in a browser" in " ".join(r["notes"])
    assert fl.parse_facebook_url("https://fb.me/xyz")["canonical"] == "https://fb.me/xyz"


def test_group_link_reports_it_cannot_be_analysed():
    r = fl.parse_facebook_url("https://www.facebook.com/groups/12345/posts/67890/")
    assert r["kind"] == "group_post"
    assert any("cannot analyse" in n for n in r["notes"])


def test_bare_handle_and_junk():
    r = fl.parse_facebook_url("@nadia.rahman")
    assert r["kind"] == "profile" and r["username"] == "nadia.rahman"
    assert fl.parse_facebook_url("not a link !!")["kind"] == "invalid"
    assert fl.parse_facebook_url("https://instagram.com/nadia")["kind"] == "unknown"
    assert fl.parse_facebook_url("") is None
    assert fl.parse_facebook_url("   ") is None


def test_url_embedded_in_pasted_text():
    r = fl.parse_facebook_url("look at this https://www.facebook.com/nadia.rahman thanks")
    assert r["username"] == "nadia.rahman"


def test_reserved_paths_are_not_usernames():
    for url in ["https://www.facebook.com/groups/123", "https://www.facebook.com/watch"]:
        r = fl.parse_facebook_url(url)
        assert r["kind"] != "profile" or r.get("username") in (None, "")


def test_owner_comparison():
    link = fl.parse_facebook_url("https://www.facebook.com/nadia.rahman")
    assert fl.compare_owner(link, "Nadia Rahman")["verdict"] == "match"
    assert fl.compare_owner(fl.parse_facebook_url("https://www.facebook.com/zuck"), "Nadia Rahman")["verdict"] == "mismatch"
    # numeric-only links cannot be compared
    assert fl.compare_owner(fl.parse_facebook_url("https://www.facebook.com/profile.php?id=1"), "Nadia Rahman")["verdict"] == "unknown"
    assert fl.compare_owner(None, "Nadia Rahman")["verdict"] == "unknown"


def test_summary_label():
    assert fl.link_summary(fl.parse_facebook_url("https://www.facebook.com/zuck")) == "profile · @zuck"
    assert fl.link_summary(fl.parse_facebook_url("https://www.facebook.com/pages/X/123456789")) == "page · X (id 123456789)"
