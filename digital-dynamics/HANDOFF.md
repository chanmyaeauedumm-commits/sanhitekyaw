# Handoff — Digital Dynamics · Facebook Analyzer

**How to use this document**

- **You (the human):** read §1–§4 for what exists, then §8 to install it.
- **Another AI:** paste the block in **§13** — it is self-contained and tells the assistant
  everything it needs: stack, versions, file map, guardrails, and how to debug.

---

## 1 · What this is, in one paragraph

A free, local **Streamlit** app that analyses a **Facebook "Download your information" export** (or a Page
you administer via the Graph API) and produces a *Transparent Possibility* report. Every finding is
rendered as **Data Fact → Logical Reasoning → Transparent Possibility → Confidence → Disclaimer**. It
measures timeline posts, Messenger threads (metadata only), the account's outgoing comments, tagged
places and the friend list; it detects routine shifts and anomalies against explicit numeric thresholds.
It never scrapes, never uses browser automation, and never needs an API key — the offline rule engine
produces the full report; a free Groq/Llama key only rewrites it as prose.

The app also accepts a **pasted Facebook profile/Page link**, which labels the analysis, cross-checks that
the export actually belongs to that account, and (for Pages) supplies the Graph API identifier.

---

## 2 · Status: what is verified, and what is not

| Area | State | Evidence |
|---|---|---|
| Export parsing (ZIP + single JSON) | ✅ verified | 17 tests + doctor dry-run on the demo archive |
| Channel mapping (posts / Messenger / comments out / places / friends) | ✅ verified | demo: 50 / 1,272 / 40 / 50 / 5 |
| Findings engine (7 findings on demo data) | ✅ verified | doctor prints the exact list, §6 |
| Offline report (no API key) | ✅ verified | 4,059-char Markdown, deterministic |
| Groq report | ⚠️ integration verified, no live success run | a bad key returns a clean 401 message; no valid key available in the build sandbox |
| Profile-link parser | ✅ verified | 12 tests, 18 URL shapes |
| Page Graph API | ⚠️ mapping unit-tested, **never run live** | needs a real Page token to confirm |
| Streamlit UI | ✅ verified headlessly (AppTest) | demo run, Messenger-off run, link-match, link-mismatch, Graph validation — 0 exceptions |
| Tests | ✅ 38 passing | 17 + 12 + 9 |

Nothing in this app has ever been pointed at a real person's data during development; all fixtures are
synthetic.

---

## 3 · Inventory

| File | Lines | Role |
|---|---|---|
| `app.py` | ~700 | Streamlit UI: link box, source picker, 6 tabs, charts, finding cards, downloads |
| `facebook.py` | ~580 | Archive scanner + classifier, channel mapping, Graph API client |
| `osint_core.py` | ~1,020 | Loaders, aggregation, co-location, thresholds, finding generator |
| `ai_analyst.py` | ~225 | Facebook-aware system prompt, Groq call, offline Markdown report |
| `facebook_links.py` | ~290 | Pasted-link parser + owner cross-check |
| `generate_mock_facebook_export.py` | ~275 | Builds the synthetic export in Meta's folder layout |
| `doctor.py` | ~250 | Environment/project self-check → `doctor_report.txt` |
| `tests/` | 3 files | 38 tests total |
| `facebook_demo_export.zip` | 21 KB | 50 posts · 1,272 messages · 40 comments · 50 places · 5 friends · 6 threads |
| `archive/` | — | Earlier Mastodon/Reddit/X/Instagram code, no longer imported (13 tests still pass) |
| `mock_data.json`, `sample_data.csv` | — | Platform-agnostic fixtures used by the core-engine tests |
| `requirements.txt` | — | `streamlit>=1.49,<2`, `plotly>=5.20`, `pandas>=2.0`, `vaderSentiment>=3.3.2`, `groq>=0.9` |

Built and verified on: Python **3.13.16**, Streamlit **1.66.1**, Plotly **6.0.1**, pandas **2.2.3**,
vaderSentiment **3.3.2**, groq **1.7.0**, pyarrow **26.0.0**. Python **3.10+** is the minimum.

---

## 4 · Architecture and data flow

```
facebook_demo_export.zip  (or your own export .zip / .json)
        │
        ▼
facebook.scan_export()          classifies every JSON in the archive by SHAPE
        │                       → posts | messages | comments | places | friends | other(ignored)
        ▼
FacebookExport dataclass        posts[], message_events[], activity_events[],
        │                       location_events[], friends[], files[], owner, notes
        ▼
facebook.build_dataset()        → core.Dataset(posts, extras{message_events, activity_events,
        │                          location_events, known_contacts, messages_included})
        ▼
osint_core.summarize()          → metrics{} + findings[]
        │                         channels · network · co-location · routine shift · anomalies
        ▼
ai_analyst.offline_report()   or   ai_analyst.groq_report()
        │                       (same numbers; LLM only rewrites, never computes)
        ▼
app.py — 6 tabs: Network · Routine Shift · Places · Transparent Possibilities · Report · Export & Method
```

Key function contracts (for debugging):

| Function | Signature | Notes |
|---|---|---|
| `facebook.scan_export` | `(filename, payload: bytes) -> FacebookExport` | never raises; errors land in `.errors` |
| `facebook.build_dataset` | `(export, include_messages=True, target="", source_name="") -> (Dataset, info)` | sets `extras`, re-attributes `@me` to the target handle only |
| `facebook.import_export` | `(filename, payload, include_messages, target) -> (Dataset, info)` | scan + build, the UI's entry point |
| `core.summarize` | `(dataset, target, recent_days=90) -> metrics` | reads `dataset.extras` if present |
| `core.prompt_payload` | `(metrics) -> dict` | the *only* thing the LLM ever sees: numbers, no raw text, no message bodies |
| `facebook_links.parse_facebook_url` | `(text) -> dict \| None` | kind, username, numeric_id, post_id, canonical, resolvable, notes |
| `facebook_links.compare_owner` | `(link, owner_name) -> {verdict, detail}` | verdict ∈ match / mismatch / unknown |

---

## 5 · Data model

Post (the core schema; `facebook.py` produces these before `core.normalize_posts` validates them):

```json
{"id":"fb_…","author":"@Nadia_Rahman","post_text":"…","timestamp":"2026-10-30T23:12:00",
 "location":"Blue Door Cafe","tags":["@Aria_Chen"],
 "comments":[{"user":"@x","text":"…","timestamp":"…"}],
 "checkins":[{"user":"@me","location":"Blue Door Cafe","timestamp":"…","basis":"tagged_post"}]}
```

`Dataset.extras` (Facebook-only channels):

```json
{"message_events":[{"user":"@Aria_Chen","timestamp":"2026-10-29T23:40:00","channel":"messages","thread":"Aria Chen","group":false,"length":22}],
 "activity_events":[{"user":"@Maya_Lin","timestamp":"…","channel":"comments_out","thread":"You commented on Maya Lin's post."}],
 "location_events":[{"user":"@Nadia_Rahman","location":"Blue Door Cafe","timestamp":"…"}],
 "known_contacts":["nadia rahman","maya lin"],
 "messages_included":true}
```

Three invariants the tests enforce:
1. **Message bodies never leave the process** — only sender, timestamp, thread, group flag, length.
2. **Tags are not check-ins** — tagged presence carries `basis: tagged_post`, and finding text says "tagged".
3. **Nothing is filled in** — Facebook exports have no timeline replies, so `comments_received` is 0 and the
   UI says so instead of substituting another channel.

---

## 6 · Repro baseline (what "working" looks like)

On the bundled demo export with the default 90-day window, `doctor.py` and the app must print these seven
findings:

| # | Category | Title | Confidence |
|---|---|---|---|
| 1 | Core Network | Primary Messenger contact: @Maya_Lin (576 msgs, 45%) | Medium |
| 2 | Routine Shift | Messenger network rotated — appeared @Aria_Chen (11→130), faded @Jonas_Weber (234→2) | Medium |
| 3 | Routine Shift | Messaging hours moved — night share 0% → 31% | Medium |
| 4 | Anomaly | High-contact accounts outside the friend list — @Aria_Chen | Low |
| 5 | Location Overlap | Repeated co-location with @Aria_Chen at Blue Door Cafe (4 posts, 10 days, `tagged_post`) | High |
| 6 | Routine Shift | Language became more inward-looking — vague 0→50%, solo 0→44%, social 12→0% | High |
| 7 | Anomaly | Late-night posting concentration — 81% vs 0% | High |

If a setup produces different numbers, the data or the versions changed — not the logic.

---

## 7 · The profile-link box (newest feature)

Sidebar → **1 · Profile link**. Paste anything Facebook-shaped; the app parses it instantly, shows the kind
and a **canonical link in a copyable code block**, and explains anything it cannot resolve.

| Pasted | Detected | Used for |
|---|---|---|
| `facebook.com/zuck` | `profile · @zuck` | target label + owner cross-check |
| `web.facebook.com/nadia.rahman/?mibextid=abc` | `profile · @nadia.rahman` | tracking params stripped |
| `profile.php?id=1000…` | `profile · id 1000…` | label; no username to cross-check |
| `pages/Name/123456789` | `page · Name (id 123456789)` | Graph API identifier (prefills the Page field) |
| `permalink.php?story_fbid=…`, `/posts/…`, `photo?fbid=…`, `watch/?v=…` | `post` | warns: content, not an account |
| `share/p/…`, `fb.me/…` | `short_link` | warns: open in a browser, copy the full URL |
| `groups/123/posts/456` | `group_post` | warns: group content is not in a personal export |
| `instagram.com/…` | `unknown` | explains it is not a Facebook domain |
| `@nadia.rahman`, `nadia.rahman` | `profile` | bare handles accepted |

Two behaviours worth knowing:

- **Owner cross-check.** After a run, the results header compares the link's username with the owner
  inferred from Messenger senders. A mismatch shows a warning: *"The link points at `zuck`, but the
  export's owner looks like `Nadia Rahman`…"* — this is how you catch a friend's archive handed over by
  mistake.
- **A link never triggers a fetch.** Pasting a profile URL does not and cannot pull data: Meta removed
  personal-profile API access in 2018. The app says so rather than pretending.

---

## 8 · Setup

### Windows (PowerShell or cmd)

```bat
cd %USERPROFILE%\Downloads\digital-dynamics
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python doctor.py
python -m streamlit run app.py
```

If activation is blocked by policy:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

### macOS / Linux

```bash
cd ~/Downloads/digital-dynamics
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python doctor.py
python -m streamlit run app.py
```

No `python3`? Install from python.org (3.12 or 3.13) or `brew install python@3.12`.

### Then

1. Browser opens `http://localhost:8501` (if not, open it manually).
2. Leave the sidebar source on **Bundled demo export** and press **▶ Run Facebook Analysis**.
   You should see 50 posts / 1,272 messages / 7 findings.
3. Paste a link in **1 · Profile link** to try the cross-check — e.g. `https://www.facebook.com/nadia.rahman`
   (match) then `https://www.facebook.com/zuck` (mismatch).

### Using your own data

Facebook → **Settings → Your Facebook Information → Download your information** → **JSON** (not HTML),
tick at least **Posts**, **Comments**, **Friends**, **Messages** (for the network channels) → date range
**All time** → *Create file*. Then sidebar → **Upload export** → drop the `.zip`.

### Optional AI report (free)

<https://console.groq.com/keys> → create key →

```bash
export GROQ_API_KEY="gsk_..."        # macOS/Linux
$env:GROQ_API_KEY="gsk_..."          # PowerShell
set GROQ_API_KEY=gsk_...             # cmd
```

…or copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` and paste it there (it is
git-ignored). Then sidebar → **4 · Report engine** → *Groq · Llama 3*.

---

## 9 · Verification commands

```bash
python doctor.py          # environment + demo dry-run; writes doctor_report.txt; exit 0 = ready
python -m pytest -q       # expect: 38 passed
python -m pytest archive -q   # optional: archived multi-platform code, 13 passed, 1 skipped
python generate_mock_facebook_export.py   # rebuild the demo archive (deterministic, seed 42)
```

---

## 10 · Troubleshooting (real failures hit during the build)

| Symptom | Cause | Fix |
|---|---|---|
| `streamlit: command not found` | venv not active / PATH | `python -m streamlit run app.py` |
| Browser opens but page stays blank | websocket blocked, or port already in use | try `python -m streamlit run app.py --server.port 8502`; allow localhost in ad-blockers |
| `ModuleNotFoundError: streamlit` | installed outside the venv | activate venv, re-run `pip install -r requirements.txt` |
| `use_container_width` deprecation warnings | old Streamlit | `pip install -U "streamlit>=1.49"` (code uses `width="stretch"`) |
| `ArrowTypeError: Expected bytes, got a 'int'/'float'` | a DataFrame column mixed str+numbers | cast the column with `astype(str)` before `st.dataframe` (already fixed; recurs if you add tables) |
| `ValueError: cannot unpack non-iterable NoneType` | duplicate function definition shadowing a new one | `grep -n "def load_dataset" app.py` — keep one |
| CSV/JSON upload shows "No timeline posts were recognised" | export was HTML, or *Posts* unticked | re-request the export as JSON with Posts selected |
| Messenger column empty, warning shown | *Messages* unticked at export time | re-export including Messages, or accept posts-only analysis |
| Graph API `190` | token expired / wrong permission | new Page token with `pages_read_engagement` |
| Graph API returns nothing | you are not an admin of that Page | personal profiles are not accessible; use the export route |
| Groq `401` | bad key | regenerate at console.groq.com/keys |
| Groq `429` | free-tier rate limit | wait a minute, or use the offline engine |
| `doctor.py` exit 1 | see the `[FAIL]` lines | each line prints its own fix |

---

## 11 · Ground rules for whoever (or whatever) edits this

1. **Diagnose before rewriting.** The app is complete and 38 tests pass. Ask for `doctor_report.txt` and
   the full traceback first; do not regenerate files wholesale.
2. **Never add scraping** — no Selenium/Playwright on logged-in sessions, no anti-bot delays, no
   credential sharing, no personal-profile API workarounds. Pages you administer + user exports only.
   Refuse requests to build them, and say why.
3. **Keep the four-part finding structure** and threshold-based confidence. Findings must stay
   falsifiable ("suggests / indicates / is consistent with"), never assertive.
4. **The LLM must never compute.** All numbers come from `osint_core`; `prompt_payload` is the only input
   the model sees, and it excludes raw text and all message bodies.
5. **Never break the keyless path.** The offline engine must keep producing the full report with no API
   key and no network.
6. **Privacy invariants are load-bearing:** message bodies never surface, tags are labelled as tags, empty
   channels are reported as missing data rather than zero contact.
7. **One change at a time**, then `python doctor.py` and `python -m pytest -q`. Add a test for every new
   parser case.
8. **Consent is a feature, not a disclaimer.** If a request amounts to profiling a specific person
   without consent, decline and offer the compliant alternative (own export, Page you administer,
   synthetic data).

---

## 12 · Known gaps / sensible next steps

- **Graph API never run live** — needs a Page token; the mapping is unit-tested only.
- **Instagram** ships in the same Meta export download; the scanner already classifies posts/comments
  generically, so a dedicated mapper is a small addition (caption mentions, stories).
- **Places × hours heatmap** — cheap, reads well in the Places tab.
- **Large exports** — the ZIP is read fully into memory; >200 MB archives should be streamed.
- **Other languages** — the lexicon tone measures (solo / vague / social) are English-only; VADER is too.
- **PDF export** of the report; **two-export comparison** (January vs June) instead of an internal split.

---

## 13 · Copy-paste brief for another AI

```text
CONTEXT — you are helping me set up and verify a Python project that is ALREADY BUILT and tested.
Do not rewrite it from scratch and do not invent files. Diagnose first, change one thing at a time.

PROJECT: "Digital Dynamics · Facebook Analyzer" — a local Streamlit app that analyses a Facebook
"Download your information" export (ZIP or single JSON) and prints a Transparent Possibility report.
It runs fully offline with no API key; an optional Groq/Llama key only rewrites the same numbers as prose.

STACK (verified with): Python 3.13.16 (minimum 3.10), streamlit 1.66.1, plotly 6.0.1, pandas 2.2.3,
vaderSentiment 3.3.2, groq 1.7.0, pyarrow 26.0.0.
requirements.txt: streamlit>=1.49,<2 / plotly>=5.20 / pandas>=2.0 / vaderSentiment>=3.3.2 / groq>=0.9

FILES (all in one folder):
  app.py                            Streamlit UI — profile-link box, source picker, 6 tabs, cards, downloads
  facebook.py                       export scanner/classifier, channel mapping, Page Graph API client
  osint_core.py                     loaders, aggregation, co-location, thresholds, finding generator
  ai_analyst.py                     Facebook-aware system prompt, Groq call, offline Markdown report
  facebook_links.py                 pasted-link parser + owner cross-check
  generate_mock_facebook_export.py  builds the synthetic demo archive
  doctor.py                         self-check → doctor_report.txt (exit 0 = ready)
  facebook_demo_export.zip          synthetic demo export (50 posts, 1272 messages, 40 comments,
                                    50 places, 5 friends, 6 threads)
  tests/                            test_facebook.py (17), test_facebook_links.py (12), test_smoke.py (9)
  archive/                          older multi-platform code, not imported by the app
  .streamlit/config.toml            theme + server settings;  .streamlit/secrets.toml.example

HOW IT WORKS (so your advice matches the code):
  export → facebook.scan_export() classifies every JSON by shape (posts | messages | comments | places |
  friends | other→ignored) → facebook.build_dataset() → core.Dataset(posts + extras{message_events,
  activity_events, location_events, known_contacts}) → osint_core.summarize() → metrics + findings →
  ai_analyst.offline_report() or .groq_report() → UI tabs.
  Key contracts: scan_export(filename, bytes) -> FacebookExport; build_dataset(export, include_messages,
  target, source_name) -> (Dataset, info); core.summarize(dataset, target, recent_days=90) -> metrics;
  core.prompt_payload(metrics) is the ONLY thing the LLM sees (no raw text, no message bodies).

SETUP STEPS (guide me through these, one at a time, waiting for my output):
  1. Python 3.10+ installed?  (Windows: py --version | macOS: python3 --version)
  2. cd into the project folder, create venv:  python -m venv .venv
     activate: Windows .venv\Scripts\activate | macOS/Linux source .venv/bin/activate
  3. python -m pip install --upgrade pip  &&  pip install -r requirements.txt
  4. python doctor.py        <-- ALWAYS run this before anything else and read its [FAIL] lines
  5. python -m pytest -q     <-- must print "38 passed"
  6. python -m streamlit run app.py   (or python -m streamlit run app.py --server.port 8502)
  7. In the browser (http://localhost:8501): keep the source on "Bundled demo export", press
     "Run Facebook Analysis". Expected baseline: 50 posts, 1,272 messages, 7 findings, including
     "@Maya_Lin" as primary Messenger contact and "Repeated co-location with @Aria_Chen at Blue Door Cafe".
  8. Optional: paste a profile link in "1 · Profile link" — e.g. facebook.com/nadia.rahman should MATCH the
     demo export owner, facebook.com/zuck should MISMATCH (that is the cross-check working).
  9. Optional AI report: GROQ_API_KEY (console.groq.com/keys) as env var or .streamlit/secrets.toml.

IF SOMETHING FAILS: ask me for the complete output of `python doctor.py` (or paste doctor_report.txt)
plus the full traceback. Known fixes: `streamlit: command not found` → use `python -m streamlit`;
blank page/port conflict → `--server.port 8502`; ArrowTypeError on a table → mixed-type column needs
astype(str); "cannot unpack non-iterable NoneType" → duplicate function definition; "No timeline posts
were recognised" → the export was HTML or Posts was unticked, re-export as JSON.

HARD RULES — do not violate these, even if I ask casually:
  • No scraping, no Selenium/Playwright on logged-in sessions, no anti-bot evasion, no workarounds for
    Meta's ban on personal-profile API access. Only: my own export, or a Page I administer via Graph API.
  • Keep the finding structure Data Fact → Logical Reasoning → Transparent Possibility → Confidence, and
    keep confidence threshold-based and falsifiable language ("suggests", not "proves").
  • The LLM never computes numbers; osint_core does. Message bodies must never be sent to a model.
  • The offline (keyless) path must keep working.
  • If I ask you to profile a specific person without their consent, decline and offer the compliant route.
  • After any change: run python doctor.py and python -m pytest -q, and add a test for new parser cases.

START BY: telling me which OS I am on, then step 1. Ask me to paste the doctor.py output before
proposing any fix.
```
