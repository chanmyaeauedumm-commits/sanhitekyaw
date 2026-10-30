"""
Digital Dynamics · Facebook Analyzer
====================================
A local, free OSINT *pattern* analyzer for Facebook data you are entitled to hold:
your own "Download your information" export, or a Page you administer.

Run:
    streamlit run app.py

Deterministic analysis (channels, network, tagged co-location, routine shift,
anomaly thresholds) + an optional Llama report via Groq. The offline engine
produces the same report structure with no API key and no network.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import ai_analyst
import facebook
import facebook_links
import osint_core as core

APP_DIR = Path(__file__).parent
DEMO_PATH = APP_DIR / "facebook_demo_export.zip"
MAX_FILE_MB = 20

st.set_page_config(page_title="Digital Dynamics · Facebook Analyzer", page_icon="📘", layout="wide")

# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #
st.markdown(
    """
<style>
  .block-container {padding-top: 2.2rem; max-width: 1250px;}
  .hero {background: linear-gradient(120deg, #0b1f3a 0%, #12386b 55%, #1877f2 100%);
         border-radius: 18px; padding: 26px 30px; color: #fff; margin-bottom: 18px;}
  .hero h1 {margin: 0 0 6px 0; font-size: 2rem; letter-spacing: -0.5px;}
  .hero p {margin: 0; opacity: .88; font-size: .95rem;}
  .badge {display:inline-block; padding: 2px 10px; border-radius: 999px;
          font-size: .74rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;}
  .b-high {background:#dcfce7; color:#166534;}
  .b-medium {background:#fef3c7; color:#92400e;}
  .b-low {background:#e2e8f0; color:#475569;}
  .card-title {font-size: 1.02rem; font-weight: 650; margin-bottom: 2px;}
  .card-cat {font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; color:#64748b;}
  .lbl {font-size:.74rem; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:#1877f2;}
  .note {font-size:.82rem; color:#64748b;}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="hero">
  <h1>Digital Dynamics · Facebook Analyzer</h1>
  <p>Facebook-only OSINT analysis: timeline, Messenger, tagged places, friend list →
  a <strong>Transparent Possibility</strong> report where every claim starts with a measured fact.</p>
</div>
""",
    unsafe_allow_html=True,
)

with st.expander("⚖️ Using this on Facebook data — read once", expanded=False):
    st.markdown(
        """
**What this app accepts**

1. **Your own "Download your information" export** (Settings → Your Facebook Information → Download).
   JSON format, Posts + Messages + Friends + Places ticked. `.zip` straight from Facebook works.
2. **A Page you administer**, via the Graph API (Page token with `pages_read_engagement`).

**What it never does**: no scraping, no browser automation, no login-as-someone, no scraping of
friends-of-friends, no bypassing privacy settings. Meta removed personal-profile API access in 2018,
which is exactly why route 1 exists.

**About Messenger**: message threads are private conversation metadata. This app counts *who, when and
how often* — it never shows you message bodies in the findings, and you should not paste an export
containing other people's private messages into a hosted deployment unless every participant is fine
with that. The *Include Messenger* toggle exists so you can analyse without it.

**Scope check before you run it**: analysing a partner, ex, colleague or stranger is surveillance, even
if the file exists. Legitimate uses are your own footprint, consented research, journalism with a
documented lawful basis, or Page analytics you own. Every output is a **lead to verify**, never proof.
        """
    )


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("1 · Profile link")
    link_input = st.text_input(
        "Paste a Facebook profile or Page link",
        value="",
        placeholder="https://www.facebook.com/your.handle",
        help="Optional. Used to label the analysis, cross-check that the export belongs to that account, "
             "and (for Pages) as the Graph API identifier. Pasting a link never scrapes anything.",
        label_visibility="collapsed",
    )
    link = facebook_links.parse_facebook_url(link_input) if link_input.strip() else None
    if link:
        if link["kind"] == "invalid":
            st.warning(link["notes"][0] if link["notes"] else "Unrecognised link.")
        elif link["kind"] == "unknown":
            st.warning(link["notes"][0] if link["notes"] else "Not a Facebook link.")
        else:
            st.markdown(
                f"**{facebook_links.link_summary(link)}**  "
                + ("· resolved" if link["resolvable"] else "· not resolvable")
            )
            if link["canonical"]:
                st.code(link["canonical"], language=None)   # has a copy button
            for note in link["notes"]:
                st.caption("• " + note)
            if link["kind"] in {"post", "group", "group_post"}:
                st.caption("This link points at content, not an account — it cannot label the target "
                           "profile. Use the account's profile link instead.")

    st.divider()
    st.header("2 · Facebook data")
    source = st.radio(
        "Source",
        ["Bundled demo export", "Upload export", "Page Graph API"],
        index=0,
        label_visibility="collapsed",
    )

    uploaded = None
    include_messages = True
    graph = {}

    if source == "Upload export":
        uploaded = st.file_uploader(
            f"Facebook export — .zip or a single .json (max {MAX_FILE_MB} MB)",
            type=["zip", "json"],
            help="The ZIP from 'Download your information' is the easiest: posts, comments, places, "
                 "friends and Messenger threads are all detected automatically.",
        )
        if uploaded is not None and uploaded.size > MAX_FILE_MB * 1024 * 1024:
            st.error(f"File is larger than {MAX_FILE_MB} MB — export a shorter date range from Facebook.")
            uploaded = None
        include_messages = st.checkbox(
            "Include Messenger threads", value=True,
            help="Message metadata (who / when / how often) is the strongest network signal in a "
                 "Facebook export. Turn it off to analyse posts and comments only.",
        )

    elif source == "Page Graph API":
        prefill = ""
        if link and link["resolvable"] and link["kind"] in {"page", "profile"}:
            prefill = link.get("numeric_id") or link.get("username") or ""
        graph["page_id"] = st.text_input(
            "Page ID or username", value=prefill,
            help="Prefilled from the link above when one was pasted.",
        )
        graph["token"] = st.text_input("Page access token", value=os.environ.get("FB_PAGE_TOKEN", ""), type="password")
        graph["limit"] = st.slider("Posts to fetch", 10, 100, 50, step=10)
        graph["max_threads"] = st.slider("Comment threads to read", 0, 50, 20, step=5)
        st.caption("Only Pages you administer. Personal profiles are not reachable this way.")
        include_messages = False

    else:
        st.caption("A synthetic Facebook export with deliberate patterns — safe to explore.")
        include_messages = st.checkbox("Include Messenger threads", value=True)

    st.divider()
    st.header("3 · Analysis")
    target_input = st.text_input(
        "Account handle",
        value="",
        placeholder="@handle (blank = detected from the export)",
        help="Blank works: the export's owner is detected from Messenger sender names.",
    )
    recent_days = st.slider("Recent window (days)", 30, 365, 90, step=15,
                            help="The 'last N days' side of every routine-shift comparison.")

    st.divider()
    st.header("4 · Report engine")
    engine = st.radio("Engine", ["Offline (rule-based, no key)", "Groq · Llama 3 (free tier)"], index=0)
    api_key, model = "", ai_analyst.DEFAULT_MODEL
    if engine.startswith("Groq"):
        api_key = st.text_input("GROQ_API_KEY", type="password", help="Or GROQ_API_KEY env var / Streamlit secret.")
        model = st.selectbox("Model", ai_analyst.MODELS, index=0)
        st.caption("Free tier at console.groq.com/keys — the offline engine needs no key.")

    st.divider()
    run = st.button("▶  Run Facebook Analysis", type="primary", width="stretch")
    st.caption("No scraping anywhere: your export, or a Page API you control.")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=600, show_spinner=False)
def _cached_graph(page_id: str, token: str, limit: int, max_threads: int):
    return facebook.graph_fetch(page_id, token, limit, max_threads)


def load_dataset() -> tuple[core.Dataset | None, dict]:
    if source == "Bundled demo export":
        if not DEMO_PATH.exists():
            st.error("facebook_demo_export.zip is missing — run `python generate_mock_facebook_export.py`.")
            return None, {}
        ds, info = facebook.import_export(
            DEMO_PATH.name, DEMO_PATH.read_bytes(), include_messages=include_messages, target=target_input
        )
        if ds is not None:
            ds.source_name = "Synthetic Facebook export · facebook_demo_export.zip"
        info["label"] = "Synthetic Facebook export (fictional people, real folder layout)"
        return ds, info

    if source == "Upload export":
        if uploaded is None:
            st.info("Choose your Facebook export (.zip or a single .json).")
            return None, {}
        ds, info = facebook.import_export(
            uploaded.name, uploaded.getvalue(), include_messages=include_messages, target=target_input
        )
        info["label"] = f"Facebook export · {uploaded.name}"
        return ds, info

    # Graph API
    if not graph.get("page_id", "").strip() or not graph.get("token", "").strip():
        st.warning("Page ID and a Page access token are both required for the Graph API route.")
        return None, {}
    posts, warnings, errors, meta = _cached_graph(
        graph["page_id"], graph["token"], graph["limit"], graph["max_threads"]
    )
    if errors:
        for e in errors:
            st.error(e)
        return None, {}
    ds = core.normalize_posts(posts, default_author="@" + graph["page_id"])
    ds.source_name = f"Facebook Page Graph API · {graph['page_id']}"
    ds.warnings.extend([w for w in warnings if w not in ds.warnings])
    return ds, {"label": f"Page Graph API · {graph['page_id']}", "platform": "facebook-graph",
                "meta": meta, "notes": warnings, "files": []}


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
CONF_CLASS = {"High": "b-high", "Medium": "b-medium", "Low": "b-low"}
PLOTLY = dict(
    template="plotly_white",
    margin=dict(l=10, r=10, t=40, b=10),
    font=dict(family="Inter, system-ui, sans-serif", size=12, color="#334155"),
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
)
BLUE, TEAL, GREY = "#1877f2", "#0e7490", "#cbd5e1"


def fig_interactants(m: dict) -> go.Figure | None:
    rows = m.get("top_interactants") or []
    if not rows:
        return None
    rows = rows[:10][::-1]
    accounts = [r["account"] for r in rows]
    fig = go.Figure()
    for key, label, color in (
        ("messages", "Messenger (incoming)", BLUE),
        ("comments_in", "Comments received", TEAL),
        ("comments_out", "Your comments to them", GREY),
    ):
        fig.add_bar(y=accounts, x=[r.get(key, 0) for r in rows], name=label, orientation="h",
                    marker_color=color)
    fig.update_layout(barmode="stack", title="Who the account interacts with, by channel",
                      height=max(320, 34 * len(rows) + 120), xaxis_title="interactions",
                      legend=dict(orientation="h", y=-0.15), **PLOTLY)
    return fig


def fig_channel_mix(m: dict) -> go.Figure:
    ch = m["channels"]
    labels = ["Incoming messages", "Posts", "Comments you wrote", "Comments received", "Location events"]
    values = [ch["messages"], ch["posts"], ch["comments_given"], ch["comments_received"], ch["location_events"]]
    fig = px.pie(names=labels, values=values, hole=0.55, title="Data channels available")
    fig.update_traces(textinfo="percent", marker=dict(line=dict(color="#fff", width=1.5)))
    fig.update_layout(height=320, showlegend=True, legend=dict(orientation="h", y=-0.12), **PLOTLY)
    return fig


def fig_hours(m: dict) -> go.Figure:
    buckets = list(m["time_buckets"].keys())

    def share(series_hours: list[dict] | list[int] | None) -> list[float]:
        return []

    msg = (m.get("messages") or {}).get("overall") or {}
    post_counts = [m["time_buckets"][b] for b in buckets]
    # recompute message buckets from partner data is not stored; use night share instead
    fig = go.Figure()
    fig.add_bar(x=buckets, y=post_counts, name="posts", marker_color=BLUE)
    if msg.get("events"):
        fig.add_trace(
            go.Scatter(
                x=[buckets[0], buckets[-1]],
                y=[0, 0],
                mode="markers",
                marker=dict(opacity=0),
                showlegend=False,
                hoverinfo="skip",
            )
        )
    fig.update_layout(title="Posting time distribution", height=300, yaxis_title="posts",
                      legend=dict(orientation="h", y=-0.25), **PLOTLY)
    return fig


def fig_message_hours(m: dict) -> go.Figure | None:
    msg = (m.get("messages") or {})
    overall, recent, earlier = msg.get("overall") or {}, msg.get("recent") or {}, msg.get("earlier") or {}
    if not overall.get("events"):
        return None
    labels = ["mean hour", "night share × 24", "unique partners"]
    fig = go.Figure()
    for prof, name, color in ((earlier, "earlier", GREY), (recent, f"last {m['recent_window_days']}d", BLUE)):
        if not prof.get("events"):
            continue
        fig.add_bar(
            x=["mean clock hour", "night hours (22–04)", "partners"],
            y=[prof["avg_hour"], (prof["night_share"] or 0) * 24, prof["partners"]],
            name=name,
            marker_color=color,
        )
    fig.update_layout(barmode="group", title="Messenger rhythm: earlier vs recent", height=300,
                      legend=dict(orientation="h", y=-0.25), **PLOTLY)
    return fig


def fig_locations(m: dict) -> go.Figure | None:
    data = m["top_locations"][:10]
    if not data:
        return None
    data = list(reversed(data))
    fig = px.bar(
        x=[c for _, c in data], y=[l for l, _ in data], orientation="h",
        title="Places the account was attached to", labels={"x": "events", "y": ""},
        color=[c for _, c in data], color_continuous_scale="Blues",
    )
    fig.update_layout(height=340, coloraxis_showscale=False, **PLOTLY)
    return fig


def fig_volume(m: dict) -> go.Figure:
    months = sorted(set(m["monthly_volume"]) | set(m["monthly_comments"]))
    fig = go.Figure()
    fig.add_bar(x=months, y=[m["monthly_volume"].get(x, 0) for x in months], name="posts", marker_color=BLUE)
    fig.add_trace(
        go.Scatter(x=months, y=[m["monthly_comments"].get(x, 0) for x in months], name="comments received",
                   mode="lines+markers", line=dict(color=TEAL, width=2.5))
    )
    fig.update_layout(title="Timeline activity by month", height=320, barmode="group",
                      legend=dict(orientation="h", y=-0.25), **PLOTLY)
    return fig


def fig_sentiment(m: dict) -> go.Figure:
    df = pd.DataFrame(m["sentiment_series"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    df["trend"] = df["sentiment"].rolling(5, min_periods=1).mean()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["date"], y=df["sentiment"], mode="markers", name="per post",
                             marker=dict(size=7, color="#94a3b8")))
    fig.add_trace(go.Scatter(x=df["date"], y=df["trend"], mode="lines", name="5-post trend",
                             line=dict(color=BLUE, width=2.5)))
    fig.add_hline(y=0, line_dash="dot", line_color="#e2e8f0")
    fig.update_layout(title="Post sentiment trend (VADER compound)", height=300,
                      legend=dict(orientation="h", y=-0.3), **PLOTLY)
    return fig


def fig_era(m: dict) -> go.Figure | None:
    e, r = m.get("earlier"), m.get("recent")
    if not e or not r:
        return None
    labels = ["night posts", "solo language", "vague language", "social language", "posting hour / 24"]
    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(r=[e["night_share"], e["solo_rate"], e["vague_rate"], e["social_rate"], e["avg_hour"] / 24],
                                  theta=labels, fill="toself", name="earlier", line=dict(color=GREY)))
    fig.add_trace(go.Scatterpolar(r=[r["night_share"], r["solo_rate"], r["vague_rate"], r["social_rate"], r["avg_hour"] / 24],
                                  theta=labels, fill="toself", name=f"last {m['recent_window_days']}d",
                                  line=dict(color=BLUE)))
    fig.update_layout(title="Timeline profile: earlier vs recent", height=360,
                      polar=dict(radialaxis=dict(range=[0, 1], showticklabels=False)),
                      legend=dict(orientation="h", y=-0.12),
                      margin=dict(l=30, r=30, t=50, b=10), template="plotly_white",
                      font=PLOTLY["font"], paper_bgcolor="rgba(0,0,0,0)")
    return fig


def render_card(f: dict) -> None:
    conf = f.get("confidence", "Low")
    with st.container(border=True):
        c1, c2 = st.columns([4, 1])
        with c1:
            st.markdown(
                f'<div class="card-cat">{f.get("category","")}</div>'
                f'<div class="card-title">{f.get("title","")}</div>',
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(
                f'<div style="text-align:right"><span class="badge {CONF_CLASS.get(conf, "b-low")}">'
                f"{conf} confidence</span></div>",
                unsafe_allow_html=True,
            )
        st.markdown(f'<div class="lbl">Data fact</div>{f.get("data_fact","")}', unsafe_allow_html=True)
        st.markdown(f'<div class="lbl">Logical reasoning</div>{f.get("logical_reasoning","")}', unsafe_allow_html=True)
        st.markdown(f'<div class="lbl">Transparent possibility</div>{f.get("possibility","")}', unsafe_allow_html=True)
        st.caption(f.get("disclaimer", core.DISCLAIMER))


# --------------------------------------------------------------------------- #
# Run pipeline
# --------------------------------------------------------------------------- #
if run:
    with st.spinner("Reading the Facebook export…"):
        ds, prov = load_dataset()
    if ds is None:
        st.stop()
    if ds.errors:
        for e in ds.errors:
            st.error(e)
        st.stop()
    if not ds.posts:
        st.error("No timeline posts were recognised. Make sure the export was requested as JSON with Posts selected.")
        st.stop()

    bar = st.progress(0, text="Step 1 of 3 · Parsing the export…")
    if target_input.strip():
        target = core.normalize_handle(target_input)
    elif link and link.get("username"):
        target = core.normalize_handle(link["username"])
    else:
        target = ds.posts[0]["author"]

    bar.progress(33, text="Step 1 of 3 · Parsing the export… done")
    bar.progress(40, text="Step 2 of 3 · Mapping Messenger, comments & places…")
    metrics = core.summarize(ds, target, recent_days)
    payload = core.prompt_payload(metrics)
    bar.progress(66, text="Step 2 of 3 · Mapping Messenger, comments & places… done")

    bar.progress(72, text="Step 3 of 3 · Generating transparent possibilities…")
    report, engine_used, ai_error = None, "offline", None
    if engine.startswith("Groq"):
        key = ai_analyst.resolve_api_key(api_key)
        if not key:
            ai_error = "No Groq API key found — fell back to the offline engine."
        else:
            report, ai_error = ai_analyst.groq_report(payload, key, model)
            engine_used = "groq" if report else "offline"
    if not report:
        report = ai_analyst.offline_report(payload)
    bar.progress(100, text="Step 3 of 3 · done")

    st.session_state["result"] = {
        "metrics": metrics,
        "report": report,
        "engine_used": engine_used,
        "ai_error": ai_error,
        "target": target,
        "warnings": ds.warnings,
        "provenance": prov,
        "link": link,
        "owner_check": facebook_links.compare_owner(link, prov.get("owner_detected")),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": ds.source_name,
    }

result = st.session_state.get("result")

if not result:
    st.info("Pick a source in the sidebar — the bundled demo export is pre-selected — then press **Run Facebook Analysis**.")
    st.markdown(
        """
| Step | What happens |
|---|---|
| 1 · Parse | Every JSON file in the export is classified: posts, Messenger threads, your comments, places, friends. Unrecognised files are listed, not silently dropped. |
| 2 · Map | Timeline → posts and locations. Tags on a post become joint presence at that venue. Messenger → interaction network. Friends → a label for "known contact". |
| 3 · Possibilities | Explicit thresholds produce findings, each rendered as Data Fact → Logical Reasoning → Transparent Possibility → Confidence. |
        """
    )
    st.stop()

m = result["metrics"]
target = result["target"]
prov = result.get("provenance") or {}

if result["ai_error"]:
    st.warning(result["ai_error"])
for w in result["warnings"]:
    st.warning(w)

engine_label = "Groq · Llama 3" if result["engine_used"] == "groq" else "Offline rule engine"
ch = m["channels"]
k = st.columns(5)
k[0].metric("Posts", ch["posts"])
k[1].metric("Messenger (in)", ch["messages"] if ch["messages_included"] else "excluded")
k[2].metric("Interactants", len(m["top_interactants"]))
k[3].metric("Friend list", len(m["known_contacts"]))
k[4].metric("Findings", len(m["findings"]))
st.caption(f"Source: **{result['source']}** · account **{target}** · engine **{engine_label}** · generated {result['generated_at']}")

link = result.get("link")
check = result.get("owner_check") or {}
if link and link.get("kind") not in {"invalid", "unknown"}:
    with st.container(border=True):
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"**Profile link under analysis** · {facebook_links.link_summary(link)}")
            if link.get("canonical"):
                st.code(link["canonical"], language=None)
        with c2:
            verdict = check.get("verdict")
            if verdict == "match":
                st.success(check.get("detail", ""))
            elif verdict == "mismatch":
                st.warning(check.get("detail", ""))
            else:
                st.info(check.get("detail", "No owner detected in the export to compare against."))

tabs = st.tabs(["📊 Network", "🕒 Routine Shift", "📍 Places", "🔎 Transparent Possibilities", "🧾 Report", "🗂 Export & Method"])

# --- Network ----------------------------------------------------------------
with tabs[0]:
    c1, c2 = st.columns([3, 2])
    with c1:
        fig = fig_interactants(m)
        if fig:
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No interactions recognised in this export.")
    with c2:
        st.plotly_chart(fig_channel_mix(m), width="stretch")

    rows = m.get("top_interactants") or []
    if rows:
        df = pd.DataFrame(rows)[["account", "messages", "comments_in", "comments_out", "total"]]
        df["in friend list"] = [
            "yes" if r["account"].lstrip("@").replace("_", " ").lower() in set(m.get("known_contacts") or []) else "no"
            for r in rows
        ]
        st.dataframe(df.rename(columns={"messages": "messenger (in)", "comments_in": "comments received",
                                        "comments_out": "your comments", "total": "total"}),
                     width="stretch", hide_index=True)
        st.caption("A blank Messenger column usually means those threads were not included in the export.")

    if m.get("messages", {}).get("overall", {}).get("events"):
        st.plotly_chart(fig_message_hours(m), width="stretch")
        mm = m["messages"]
        cols = st.columns(3)
        for col, key, label in ((cols[0], "overall", "Whole export"), (cols[1], "earlier", "Earlier window"),
                                (cols[2], "recent", f"Last {m['recent_window_days']} days")):
            prof = mm.get(key) or {}
            with col:
                st.markdown(f"**{label}**")
                st.caption(
                    f"{prof.get('events', 0)} incoming · {prof.get('partners', 0)} partners · "
                    f"mean {core.hour_str(prof['avg_hour']) if prof.get('avg_hour') is not None else '—'} · "
                    f"night {(prof.get('night_share') or 0):.0%}"
                )
                top = (prof.get("top_partners") or [])[:3]
                if top:
                    st.caption("top: " + ", ".join(f"{u} ({c})" for u, c in top))

# --- Routine shift ----------------------------------------------------------
with tabs[1]:
    if not m.get("earlier") or not m.get("recent"):
        st.info(f"Not enough posts on both sides of the {recent_days}-day split to compare periods.")
    else:
        e, r = m["earlier"], m["recent"]
        c1, c2 = st.columns([3, 2])
        with c1:
            fig = fig_era(m)
            if fig:
                st.plotly_chart(fig, width="stretch")
        with c2:
            st.dataframe(
                pd.DataFrame(
                    {
                        "metric": ["mean posting hour", "night share", "mean sentiment", "avg words",
                                   "solo language", "vague language", "social language", "posts"],
                        "earlier": [core.hour_str(e["avg_hour"]), f"{e['night_share']:.0%}", f"{e['avg_sentiment']:+.2f}",
                                    f"{e['avg_words']:.1f}", f"{e['solo_rate']:.0%}", f"{e['vague_rate']:.0%}",
                                    f"{e['social_rate']:.0%}", str(e["posts"])],
                        f"last {m['recent_window_days']}d": [core.hour_str(r["avg_hour"]), f"{r['night_share']:.0%}",
                                                             f"{r['avg_sentiment']:+.2f}", f"{r['avg_words']:.1f}",
                                                             f"{r['solo_rate']:.0%}", f"{r['vague_rate']:.0%}",
                                                             f"{r['social_rate']:.0%}", str(r["posts"])],
                    }
                ),
                width="stretch", hide_index=True,
            )
        st.plotly_chart(fig_sentiment(m), width="stretch")
        st.plotly_chart(fig_volume(m), width="stretch")

# --- Places -----------------------------------------------------------------
with tabs[2]:
    c1, c2 = st.columns(2)
    with c1:
        fig = fig_locations(m)
        if fig:
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No places in this export — Facebook only includes them for posts with a location or check-in.")
    with c2:
        st.plotly_chart(fig_hours(m), width="stretch")
        st.caption(
            "Facebook has no independent venue check-in feed: places come from the `place` field on posts "
            "plus the places-visited file. Joint presence is inferred from **tags on posts at that venue**."
        )

    st.subheader("Co-location episodes")
    details = m["co_location"]["details"]
    if details:
        df = pd.DataFrame(details)[["user", "location", "date", "target_ts", "other_ts", "gap_hours", "basis"]]
        st.dataframe(df.rename(columns={"user": "account", "location": "venue", "target_ts": "post time",
                                        "other_ts": "their time", "gap_hours": "gap (h)",
                                        "basis": "basis"}),
                     width="stretch", hide_index=True)
        st.caption("`tagged_post` = you tagged them at that venue. `checkin` / `post_place` = independent venue data.")
    else:
        st.info("No shared-venue events: no post tagged another account at a place.")

# --- Possibilities ----------------------------------------------------------
with tabs[3]:
    st.markdown('<div class="note">Each card is one pattern: the fact is measured, the possibility is a '
                "hedged reading, the confidence comes from explicit thresholds.</div>", unsafe_allow_html=True)
    for cat in ["Core Network", "Location Overlap", "Routine Shift", "Anomaly", "Method"]:
        items = [f for f in m["findings"] if f.get("category") == cat]
        if not items:
            continue
        st.markdown(f"#### {cat}")
        for f in items:
            render_card(f)

    st.divider()
    c1, c2 = st.columns([1, 1])
    with c1:
        st.download_button("⬇ Report (.md)", data=result["report"].encode("utf-8"),
                           file_name=f"facebook_possibilities_{target.strip('@')}.md",
                           mime="text/markdown", width="stretch")
    with c2:
        st.download_button("⬇ Findings (.json)",
                           data=json.dumps({"target": target, "findings": m["findings"],
                                            "metrics": core.prompt_payload(m)}, indent=2, default=str).encode("utf-8"),
                           file_name=f"facebook_findings_{target.strip('@')}.json",
                           mime="application/json", width="stretch")

# --- Report -----------------------------------------------------------------
with tabs[4]:
    st.markdown(result["report"])
    st.caption(f"Engine: {engine_label}. The offline engine reproduces the same structure with no model, "
               "so the report is auditable end to end.")

# --- Export & Method --------------------------------------------------------
with tabs[5]:
    files = prov.get("files") or []
    if files:
        st.subheader("Files recognised in the export")
        df = pd.DataFrame(files)
        df["path"] = df["path"].str.replace(r"^.*?/", "", regex=True)
        st.dataframe(df.rename(columns={"path": "file (tail)", "kind": "detected as", "label": "meaning",
                                        "entries": "entries"}),
                     width="stretch", hide_index=True)
        ignored = [f for f in files if f["kind"] == "other"]
        if ignored:
            st.caption(f"{len(ignored)} file(s) were recognised but deliberately ignored (reactions, ads, "
                       "search history, etc.). Nothing is guessed from them.")

    st.subheader("Provenance")
    if prov.get("label"):
        st.markdown(f"**Source:** {prov['label']}")
    summary = {k: prov.get(k) for k in ("owner_detected", "target_handle", "posts", "messages",
                                        "activity_events", "locations", "friends", "messages_included")
               if prov.get(k) is not None}
    if link:
        summary["pasted_link"] = link.get("canonical") or link.get("raw")
        summary["pasted_link_kind"] = link.get("kind")
    if summary:
        st.json(summary, expanded=False)
    for note in prov.get("notes", []):
        st.caption(f"• {note}")

    st.subheader("Data quality")
    dq = m["data_quality"]
    q = st.columns(4)
    q[0].metric("Posts with a place", dq["posts_with_location"])
    q[1].metric("Posts with tags", sum(1 for p in m["raw_posts"] if p["tags"]))
    q[2].metric("Commenters", dq["unique_commenters"])
    q[3].metric("Friend list", len(m["known_contacts"]))

    st.subheader("Method")
    st.markdown(
        f"""
1. **Acquisition.** Facebook export (.zip / .json) or a Page you administer via Graph API. Every JSON in
   the archive is classified by shape: posts, Messenger threads, your comments, places, friends — anything
   else is listed as ignored. No scraping, no browser automation.
2. **Mapping.** Timeline posts → text, timestamp, `place`, tags. A post that tags someone at a venue
   becomes a shared-presence event (`basis = tagged_post`). Messenger → incoming/outgoing interaction
   events with sender and timestamp. Friends → names used only as a "known contact" label.
3. **Aggregation.** Interaction totals per account split by channel; hour buckets (morning 05–11,
   afternoon 12–16, evening 17–21, night 22–04); monthly post/comment volume; venue frequency including
   the places-visited file.
4. **Routine shift.** Timeline split at `last post − {m['recent_window_days']} days`
   (cutoff **{m['recent_cutoff']}**). Compared: mean posting hour, night share, VADER sentiment, mean words,
   solo / vague / social lexicon rates — and separately the Messenger rhythm and partner mix.
5. **Anomaly thresholds.** Posting hour delta ≥ 1.5 h · night-share delta ≥ 25 pts · comment-share
   rotation ≤ 40% of former share · sentiment delta ≥ 0.12 · vague/solo delta ≥ 15 pts · Messenger
   partner share doubling/halving · high-contact accounts missing from the friend list.
6. **Report.** Findings render as Data Fact → Logical Reasoning → Transparent Possibility → Confidence.
   The optional LLM step reuses these numbers and is instructed to invent none.
        """
    )

    with st.expander("Raw normalised posts"):
        st.dataframe(
            pd.DataFrame(
                [
                    {"timestamp": p["timestamp"], "place": p["location"], "text": p["text"][:90],
                     "tags": ", ".join(p["tags"]), "check-ins": len(p["checkins"])}
                    for p in m["raw_posts"]
                ]
            ),
            width="stretch", hide_index=True,
        )

st.divider()
st.caption("Digital Dynamics · Facebook Analyzer · everything runs locally unless you enable the Groq "
           "engine · patterns are leads to verify, never conclusions about a person.")
