# Digital Dynamics · Facebook Analyzer

A free, local **pattern** analyzer for Facebook data you are entitled to hold — your own
"Download your information" export, or a Page you administer. It measures channels, networks, tagged
places, routine shifts and anomalies, then renders each pattern as a **Transparent Possibility**:

> **Data Fact** → **Logical Reasoning** → **Transparent Possibility** → **Confidence** → **Disclaimer**

No scraping, no browser automation, no borrowed sessions. Everything runs on your machine unless you
switch the report engine to Groq.

| Engine | Needs | Notes |
|---|---|---|
| **Offline rule engine** | nothing | Default. Deterministic Markdown report, auditable line by line. |
| **Groq · Llama 3** | free `GROQ_API_KEY` | Rewrites the same findings as prose. Instructed to reuse the computed numbers, invent none, never quote message content. Falls back to offline on any error. |

---

## 1 · Quick start

```bash
cd digital-dynamics
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python generate_mock_facebook_export.py               # optional: rebuild the demo archive
streamlit run app.py                                  # opens http://localhost:8501
```

The bundled `facebook_demo_export.zip` is a **synthetic** Facebook archive (fictional people, Meta's
real folder layout) built with deliberate patterns, so the app is fully explorable before you touch a
real export. Press **Run Facebook Analysis** with the demo source selected.

Tests:

```bash
python doctor.py             # environment + demo dry-run; writes doctor_report.txt
python -m pytest -q          # 38 offline tests
```

---

## 2 · The three data routes

| Route | What it is | Needs |
|---|---|---|
| **Bundled demo export** | synthetic `.zip` in Meta's layout | nothing |
| **Upload export** | your own Facebook "Download your information" file — `.zip` or a single `.json` | nothing |
| **Page Graph API** | Page feed + comments + places | a Page token with `pages_read_engagement` |

### The profile-link box

Sidebar → **1 · Profile link** accepts anything Facebook-shaped and tells you what it is:

| Pasted | Detected | Effect |
|---|---|---|
| `facebook.com/zuck` | profile · @zuck | labels the target, cross-checks it against the export's owner |
| `web.facebook.com/name/?mibextid=…` | profile · @name | tracking params stripped, canonical link shown in a copyable block |
| `pages/Name/123456` | page · Name (id 123456) | prefills the Graph API Page field |
| `/posts/…`, `photo?fbid=…`, `watch/?v=…` | post | warns: that is content, not an account |
| `share/p/…`, `fb.me/…` | short link | warns: open it in a browser and paste the full URL |
| `instagram.com/…` | unknown | explains it is not a Facebook domain |

Two things it deliberately does **not** do: it never fetches anything (Meta removed personal-profile API
access in 2018), and it never silently accepts a mismatch — if the link says *zuck* but the Messenger
senders say *Nadia Rahman*, you get a warning that you may have the wrong archive.

### Getting your export right

Facebook → Settings → **Your Facebook Information** → *Download your information* → **JSON** format
(not HTML) → select at least **Posts**, **Comments**, **Friends**, and **Messages** if you want the
network channels → date range *All time* → *Create file*.

The importer is structure-tolerant: it walks every JSON in the archive, classifies each file by its
shape (not just its path), and lists what it recognised **and what it ignored** in the *Export & Method*
tab. HTML-only exports are rejected with a clear message instead of a stack trace.

---

## 3 · What the parser reads

| File group | Facebook path (typical) | Becomes |
|---|---|---|
| Timeline posts | `your_activity_across_facebook/posts/your_posts__check_ins__photos_and_videos_*.json` | post text, timestamp, `place`, tags |
| Places visited / check-ins | `.../places/places_visited*.json` | location events |
| Your comments on others' posts | `.../comments/comments*.json` | outgoing interaction channel (`"You commented on X's post."` → X) |
| Messenger threads | `your_messages/inbox/*/message_*.json` | incoming/outgoing interaction events (sender + timestamp only) |
| Friends | `friends_and_followers/friends.json` | known-contact label |
| Reactions, ads, search history, friend requests | anywhere | **ignored on purpose**, listed as `other` |

Three design rules, all visible in the UI:

1. **Nothing is invented.** Facebook exports contain no other people's timeline comments — so the
   "comments received" channel is honestly zero for exports, and the network is built from Messenger and
   your outgoing comments. The app says so instead of quietly substituting something else.
2. **Tags are not check-ins.** A post that tags someone at a venue becomes shared presence with
   `basis = tagged_post`, and the finding wording says "tagged", not "checked in".
3. **Message bodies never leave the app.** Only sender, timestamp, thread size and length are used. A test
   asserts that no message text can reach the LLM payload.

---

## 4 · What gets computed

| Analysis | Method |
|---|---|
| Channels | Posts · incoming messages · outgoing comments · comments received · location events, with a per-account split |
| Network | Interaction totals per account per channel; top 12; "in friend list?" label from the friends file |
| Messenger rhythm | Incoming-message profile (count, partners, mean clock hour, night share) for the whole export, the earlier window and the last *N* days |
| Places | Venue frequency from post `place` + places-visited file |
| Co-location | Same venue, within 3 h, from post places and tags; episodes clustered per (account, venue); basis recorded |
| Routine shift | Split at `last post − N days` (default 90): mean posting hour, night share, VADER sentiment, mean words, solo / vague / social lexicon rates; plus Messenger partner mix and hours |
| Anomaly | posting hour Δ ≥ 1.5 h · night-share Δ ≥ 25 pts · comment-share rotation ≤ 40% of former share · sentiment Δ ≥ 0.12 · vague/solo Δ ≥ 15 pts · Messenger partner share doubling/halving · night-messaging Δ ≥ 20 pts · high-contact accounts missing from the friend list |

Confidence is threshold-based, not vibes — the exact rule is printed in the *Export & Method* tab.

---

## 5 · Files

```
digital-dynamics/
├── app.py                          # Facebook-only Streamlit UI (6 tabs) + profile-link box
├── facebook.py                     # export scanner, channel mapping, Graph API client
├── facebook_links.py               # pasted-link parser + owner cross-check
├── doctor.py                       # environment self-check (run this first)
├── HANDOFF.md                      # full summary + setup brief for another AI
├── osint_core.py                   # loaders, aggregation, co-location, findings engine
├── ai_analyst.py                   # system prompt (Facebook-aware), Groq call, offline report
├── generate_mock_facebook_export.py# synthetic DYI archive generator
├── facebook_demo_export.zip         # the generated demo archive
├── tests/test_facebook.py          # Facebook layer tests
├── tests/test_smoke.py             # engine-level tests (generic schema fixtures)
├── mock_data.json / sample_data.csv# platform-agnostic fixtures for the core engine
├── archive/                        # earlier multi-platform connectors (Mastodon, Reddit, X, IG)
├── requirements.txt
├── pytest.ini
└── .streamlit/config.toml
```

`archive/` holds the earlier Mastodon / Reddit / X / Instagram code. It is no longer imported by the app;
its tests still run with `python -m pytest archive -q` if you want that route back.

---

## 6 · Deploy free on Streamlit Community Cloud

```bash
git init && git add . && git commit -m "Facebook Analyzer"
git branch -M main
git remote add origin https://github.com/<you>/facebook-analyzer.git
git push -u origin main
```

<https://share.streamlit.io> → *Create app* → repo → `app.py` → *Deploy*. Then App → *Settings* →
*Secrets* (optional, AI engine only):

```toml
GROQ_API_KEY = "gsk_your_key_here"
# FB_PAGE_TOKEN = "..."   # only for the Page Graph route
```

**Before you deploy publicly, think about this**: a hosted app means the export you upload — including
Messenger metadata — is processed on someone else's machine. For anything real, run it locally, or use
the *Include Messenger threads* toggle off, or strip the messages folder from the ZIP.

| Symptom | Fix |
|---|---|
| "No timeline posts were recognised" | Export was HTML, or *Posts* was not ticked — redo as JSON |
| Messenger column empty | *Messages* was not ticked at export time (the app warns about this) |
| Graph API 190 | Token expired or lacks `pages_read_engagement` |
| Graph API 200 / empty | You are not an admin of that Page — personal profiles are not accessible |
| `ModuleNotFoundError` on Cloud | Pin deps in `requirements.txt`, then *Reboot app* |
| App sleeps | Community Cloud idles apps; open the URL to wake it |

---

## 7 · Ethics and scope

This is a Facebook-only tool because Facebook is where the data most people actually have is deepest:
timeline, private threads, friend graph and places in one archive. That depth is also why the guardrails
matter more than the features.

- **Consent first.** The export exists for the person it describes. Analysing a partner, ex, colleague or
  stranger is surveillance, whatever the file is called.
- **Messenger is private conversation metadata.** The app counts who/when/how often and never surfaces
  message bodies. Do not quote them back to anyone.
- **No non-consensual build-out.** The app refuses to output addresses, real names, health, orientation,
  religion, ethnicity, immigration status or criminal accusations, and the LLM prompt enforces it.
- **A pattern is a lead.** Every card says so, because a tag, a routine shift and a busy thread all have
  innocent explanations that the data cannot rule out.
- **Rate and retain.** Delete the export when you are done; do not keep derived JSON around longer than
  the question needs.

Compliant uses: your own footprint, consented research, journalism with a documented lawful basis, and
Page analytics you own.

---

## 8 · Limits worth reading before you trust a card

- Facebook captions are **curated performance**; tone measures read presentation, not mood.
- VADER and keyword rates mishandle sarcasm, memes, dialect and non-English text.
- `tagged_post` presence means the account owner *claimed* shared presence — tags can be added later.
- The friend list is a coarse, rename-prone map; "not in friend list" is the weakest flag in the app.
- Messenger metadata shows contact frequency, not closeness: families coordinate logistics, coworkers
  message daily and never speak socially.
- Deleted posts, private groups, DMs from other apps and everything offline are invisible here.

---

## 9 · Next steps if you want them

- **Instagram** (Meta ships it in the same export download) — the scanner already classifies
  posts/comments generically; a dedicated mapper would add caption mentions and stories.
- **Message-level tone** — deliberately not implemented; it would cross from metadata to content.
- **Timeline visualisation** — a monthly heatmap of places × hours is cheap and reads well.
