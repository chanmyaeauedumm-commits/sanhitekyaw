# Archived: multi-platform connectors

This folder holds the earlier Mastodon / Reddit / X / Instagram support that the app used before it was
narrowed to Facebook-only. Nothing here is imported by `app.py`.

- `connectors.py` — Mastodon public API + Reddit OAuth fetch, with pure mapping functions.
- `importers.py` — X archive (`tweets.js`), Instagram `posts_1.json`, Facebook posts export.
- `test_connectors.py` — their tests plus `fixtures/`.

The Facebook logic that used to live in `importers.py` has been rewritten and expanded in
`../facebook.py` (whole-archive scanning, Messenger channel, places, friends, tagged-post presence).

Run the archived tests (pytest skips this folder by default):

    python -m pytest archive -q

Note: `test_connectors.py` imports from this directory, so run it from there or with the path above.
