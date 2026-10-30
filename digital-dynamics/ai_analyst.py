"""
ai_analyst.py
=============
Two report engines behind one interface:

1. `groq_report`  — Llama via Groq's free tier (needs GROQ_API_KEY).
2. `offline_report` — fully deterministic Markdown built from the rule-based
   findings, so the app is 100% functional with no API key at all.

The LLM is never allowed to be the source of numbers: it receives the computed
metrics and the rule-based findings, and is instructed to reuse those numbers
exactly and to invent nothing.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

DEFAULT_MODEL = "llama-3.3-70b-versatile"
MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-20b",
]

SYSTEM_PROMPT = """You are an OSINT Behavioural Analyst producing a "Transparent Possibility" report.

You receive (a) computed metrics about one **Facebook** account and (b) rule-based findings
derived from those metrics. Write an analytical report an investigator could audit line by line.

NON-NEGOTIABLE RULES
1. NEVER state a possibility as an absolute fact. Use "suggests", "indicates", "is consistent with",
   "may reflect". Never write "is", "proves", "confirms", "definitely" about behaviour or intent.
2. NEVER invent numbers, usernames, locations or events. Use only the figures in the input. If a
   needed number is missing, say the data does not support that analysis. Do not do arithmetic on
   numbers that are not already in the input.
3. EVERY finding must use this exact four-part structure:
   **Data Fact:** what the raw data literally says (numbers, counts, dates).
   **Logical Reasoning:** why these data points connect to each other.
   **Transparent Possibility:** what the pattern may suggest, hedged, with the most likely
     alternative explanations named.
   **Confidence:** Low / Medium / High, with one clause explaining why that level.
4. Close the report with a **Limitations & Disclaimer** section: this is based only on the uploaded
   public digital footprint; missing, deleted, private and offline context is invisible; a pattern is
   a lead to verify, not a conclusion.
5. REFUSE to output any of the following, and say so plainly if the data seems to invite it:
   home addresses or precise whereabouts, real names behind handles, health or medical status,
   sexual orientation, religion, ethnicity, political affiliation, immigration status, criminal
   accusations, or anything about minors. Describe patterns, not private facts.
6. Do not write surveillance instructions, stalking guidance, or how to locate a person.
7. Treat the subject as a person with rights: write the report as if they may read it. Prefer
   phrasing that a reviewer or ethics board would accept.
8. Output clean Markdown only. No preamble, no "as an AI", no code fences.

FACEBOOK-SPECIFIC CONTEXT
- The data comes from a Facebook "Download your information" export (or a Page you administer via Graph
  API). Facebook exports contain NO other people's comments on the timeline, so the interaction network
  comes from Messenger threads and the account's own outgoing comments. Say so when the channel matters.
- Messenger threads are **private messages**. Treat them as the most sensitive material here: describe
  patterns of contact (who, how often, which hours) and never quote or characterise message content.
- A post that tags someone at a venue is a claim of shared presence by the account owner, not an
  independent check-in. Weight it as such.
- The friend list is used only as a label for "known contact" — never to infer anything on its own.
- If a channel is empty (e.g. no Messenger threads in the export), state plainly that the data was not
  included rather than treating it as an absence of contact.

REPORT STRUCTURE
# Transparent Possibility Report
**Target handle:** … | **Posts analysed:** … | **Observation window:** …
## 1. Core Network Analysis (Messenger + comments)
## 2. Location Overlap Detection (tagged posts / places)
## 3. Routine Shift Detection
## 4. Anomaly Detection
## 5. What Would Change This Assessment
(2–4 concrete checks or extra data that would confirm or falsify the possibilities above)
## Limitations & Disclaimer

If a section has no supporting data, write one sentence saying so instead of padding it."""


def build_user_prompt(payload: dict) -> str:
    return (
        "Here is the computed analysis payload. Use its numbers exactly and do not add any.\n\n"
        "```json\n"
        + json.dumps(payload, indent=2, default=str, ensure_ascii=False)
        + "\n```\n\n"
        "Write the Transparent Possibility report following the system rules. Only analyse the four "
        "areas that the payload actually supports; state plainly where data is too thin."
    )


def resolve_api_key(ui_key: str | None = None) -> str | None:
    """Env var -> Streamlit secrets -> sidebar input. Never hardcoded."""
    if ui_key and ui_key.strip():
        return ui_key.strip()
    env = os.environ.get("GROQ_API_KEY")
    if env and env.strip():
        return env.strip()
    try:  # Streamlit secrets, when running on Community Cloud
        import streamlit as st

        if "GROQ_API_KEY" in st.secrets:
            return str(st.secrets["GROQ_API_KEY"]).strip()
    except Exception:
        pass
    return None


def groq_report(payload: dict, api_key: str, model: str = DEFAULT_MODEL, temperature: float = 0.2) -> tuple[str | None, str | None]:
    """Returns (markdown, error)."""
    try:
        from groq import Groq
    except ImportError:
        return None, "The `groq` package is not installed. Run: pip install groq"

    try:
        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=2400,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(payload)},
            ],
        )
        text = (completion.choices[0].message.content or "").strip()
        if not text:
            return None, "Groq returned an empty response. Try again or switch model."
        return text, None
    except Exception as exc:  # network, auth, rate limit, model decommissioned…
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "invalid api key" in low or "authentication" in low:
            msg = "Groq rejected the API key (401). Check the key at console.groq.com/keys."
        elif "429" in msg or "rate limit" in low:
            msg = "Groq rate limit hit (429). The free tier is limited — wait a minute or use the offline engine."
        elif "model" in low and ("not found" in low or "decommission" in low or "does not exist" in low):
            msg = f"Model '{model}' is unavailable on your account. Pick another model in the sidebar."
        return None, msg


def offline_report(payload: dict) -> str:
    """Deterministic report from rule-based findings — no key, no network."""
    channels = payload.get("channels") or {}
    findings = payload.get("rule_based_findings", [])
    span = payload.get("span", {})
    lines = [
        "# Transparent Possibility Report",
        f"**Target handle:** {payload.get('target', 'n/a')} | "
        f"**Posts analysed:** {payload.get('n_posts', 0)} | "
        f"**Comments analysed:** {payload.get('n_comments', 0)} | "
        f"**Observation window:** {span.get('first', '?')} → {span.get('last', '?')}",
        f"**Channels:** {channels.get('posts', 0)} posts · "
        f"{channels.get('messages', 0)} incoming messages · "
        f"{channels.get('comments_given', 0)} outgoing comments · "
        f"{channels.get('location_events', 0)} location events"
        + ("" if channels.get("messages_included", True) else " · Messenger excluded"),
        "",
        "_Generated by the offline rule engine — no AI model involved. Every number below is "
        "recomputed from the uploaded file._",
        "",
    ]

    sections = {
        "Core Network": "## 1. Core Network Analysis",
        "Location Overlap": "## 2. Location Overlap Detection",
        "Routine Shift": "## 3. Routine Shift Detection",
        "Anomaly": "## 4. Anomaly Detection",
    }
    grouped: dict[str, list[dict]] = {k: [] for k in sections}
    for f in findings:
        grouped.setdefault(f.get("category", "Anomal" + "y"), []).append(f)

    for category, heading in sections.items():
        lines.append(heading)
        items = grouped.get(category, [])
        if not items:
            lines.append("_No signal strong enough in this category for the uploaded data._")
            lines.append("")
            continue
        for f in items:
            lines += [
                f"### {f.get('title', 'Finding')}",
                f"**Data Fact:** {f.get('data_fact', '')}",
                "",
                f"**Logical Reasoning:** {f.get('logical_reasoning', '')}",
                "",
                f"**Transparent Possibility:** {f.get('possibility', '')}",
                "",
                f"**Confidence:** {f.get('confidence', 'Low')} — assigned by explicit thresholds on the "
                "underlying counts, not by judgement.",
                "",
            ]

    lines += [
        "## 5. What Would Change This Assessment",
        "- A longer export (12+ months with complete comment threads) would separate a real routine "
        "change from a sampling gap.",
        "- Confirmation of whether check-ins are self-reported or third-party tagged would change how "
        "much weight the co-location pattern deserves.",
        "- Reciprocal interaction (does the other account reply to the target, or only comment?) would "
        "distinguish one-sided interest from mutual contact.",
        "- Direct confirmation from the account owner outranks every inference in this report.",
        "",
        "## Limitations & Disclaimer",
        "Based only on the uploaded public digital footprint. Absence of evidence is not evidence of "
        "absence: deleted posts, private accounts, DMs, and all offline context are invisible here. "
        "Lexicon-based tone measures (VADER sentiment, keyword rates) are crude proxies and misfire on "
        "sarcasm, memes and non-English text. Every item above is a lead to verify — never a "
        "conclusion about a person.",
        "",
        f"_{datetime.now().strftime('%Y-%m-%d %H:%M')} · offline engine · "
        f"{len(findings)} finding(s)_",
    ]
    return "\n".join(lines)


def is_ai_text(text: str) -> bool:
    return bool(text) and "offline rule engine" not in text
