"""
doctor.py — environment & project self-check for the Facebook Analyzer.

Run this first whenever something breaks:

    python doctor.py

It prints a checklist and writes the same output to `doctor_report.txt`, which you
can paste into any assistant (or into a bug report) so nobody has to guess your
Python version, packages or file layout.

Exit code 0 = ready to run. 1 = something needs fixing (each problem is labelled).
"""

from __future__ import annotations

import importlib
import os
import platform
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED_PACKAGES = [
    ("streamlit", "streamlit>=1.49,<2"),
    ("plotly", "plotly>=5.20"),
    ("pandas", "pandas>=2.0"),
    ("vaderSentiment", "vaderSentiment>=3.3.2"),
    ("groq", "groq>=0.9"),
    ("pyarrow", "pyarrow (installed with streamlit)"),
]
REQUIRED_FILES = [
    "app.py",
    "facebook.py",
    "osint_core.py",
    "ai_analyst.py",
    "generate_mock_facebook_export.py",
    "requirements.txt",
    "facebook_demo_export.zip",
    ".streamlit/config.toml",
]

lines: list[str] = []
problems: list[str] = []


def say(msg: str = "") -> None:
    print(msg)
    lines.append(msg)


def ok(label: str, detail: str = "") -> None:
    say(f"  [ OK ] {label}" + (f" — {detail}" if detail else ""))


def warn(label: str, detail: str = "", fix: str = "") -> None:
    say(f"  [WARN] {label}" + (f" — {detail}" if detail else ""))
    if fix:
        say(f"         fix: {fix}")


def bad(label: str, detail: str = "", fix: str = "") -> None:
    say(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    if fix:
        say(f"         fix: {fix}")
    problems.append(label)


say("=" * 78)
say("Facebook Analyzer · doctor")
say(f"run at {platform.node()} · {platform.system()} {platform.release()}")
say("=" * 78)

# --------------------------------------------------------------------------- #
# 1. Python
# --------------------------------------------------------------------------- #
say("\n1 · Python")
ver = sys.version_info
say(f"  interpreter: {sys.executable}")
say(f"  version:     {platform.python_version()}")
if ver >= (3, 10):
    ok("Python 3.10+")
else:
    bad("Python too old", f"{platform.python_version()} < 3.10", "install Python 3.10+ from python.org")

in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
if in_venv:
    ok("virtual environment", sys.prefix)
else:
    warn("no virtual environment active", "packages install globally",
         "python -m venv .venv && source .venv/bin/activate   (Windows: .venv\\Scripts\\activate)")

# --------------------------------------------------------------------------- #
# 2. Packages
# --------------------------------------------------------------------------- #
say("\n2 · Packages")
missing = []
for module, spec in REQUIRED_PACKAGES:
    try:
        mod = importlib.import_module(module)
        version = getattr(mod, "__version__", None)
        if version is None:
            try:
                import importlib.metadata as md

                version = md.version(module)
            except Exception:
                version = "?"
        ok(module, str(version))
    except Exception as exc:
        missing.append(module)
        bad(f"{module} import failed", str(exc), f"pip install '{spec}'")
if not missing:
    ok("all required packages present")

# --------------------------------------------------------------------------- #
# 3. Project files
# --------------------------------------------------------------------------- #
say("\n3 · Project files")
say(f"  project folder: {ROOT}")
for name in REQUIRED_FILES:
    path = ROOT / name
    if path.exists():
        ok(name, f"{path.stat().st_size:,} bytes")
    elif name == "facebook_demo_export.zip":
        warn(name, "missing",
             "python generate_mock_facebook_export.py")
    else:
        bad(name, "missing", "re-download / restore the project files")

# --------------------------------------------------------------------------- #
# 4. Imports of the app's own modules
# --------------------------------------------------------------------------- #
say("\n4 · App modules")
sys.path.insert(0, str(ROOT))
modules = {}
for name in ("osint_core", "facebook", "ai_analyst"):
    try:
        modules[name] = importlib.import_module(name)
        ok(name)
    except Exception as exc:
        bad(f"{name} failed to import", str(exc))
        say("         traceback:")
        for line in traceback.format_exc().splitlines()[-6:]:
            say(f"           {line}")

# --------------------------------------------------------------------------- #
# 5. End-to-end dry run on the demo export
# --------------------------------------------------------------------------- #
say("\n5 · Dry run on the demo export")
demo = ROOT / "facebook_demo_export.zip"
if "facebook" not in modules or "osint_core" not in modules:
    warn("skipped", "modules failed to import above")
elif not demo.exists():
    warn("skipped", "facebook_demo_export.zip missing")
else:
    try:
        import facebook
        import osint_core as core

        exp = facebook.scan_export(demo.name, demo.read_bytes())
        kinds = sorted({f["kind"] for f in exp.files})
        say(f"  files classified: {', '.join(kinds)}")
        say(f"  posts={len(exp.posts)} messages={len(exp.message_events)} "
            f"outgoing_comments={len(exp.activity_events)} places={len(exp.location_events)} "
            f"friends={len(exp.friends)} owner={exp.owner}")
        if exp.errors:
            bad("scan_export reported errors", "; ".join(exp.errors))
        else:
            ok("export scanned")

        ds, info = facebook.build_dataset(exp, include_messages=True)
        if not ds.ok:
            bad("dataset build failed", str(ds.errors))
        else:
            metrics = core.summarize(ds, ds.posts[0]["author"], 90)
            ok("analysis ran", f"{metrics['n_posts']} posts, "
                                f"{metrics['channels']['messages']} messages, "
                                f"{len(metrics['findings'])} findings")
            for f in metrics["findings"]:
                say(f"         · [{f['confidence']:<6}] {f['title']}")
            import ai_analyst

            report = ai_analyst.offline_report(core.prompt_payload(metrics))
            if report.startswith("# Transparent Possibility Report"):
                ok("offline report rendered", f"{len(report):,} chars")
            else:
                bad("offline report looked wrong", report[:80])
    except Exception as exc:
        bad("dry run crashed", f"{type(exc).__name__}: {exc}")
        for line in traceback.format_exc().splitlines()[-8:]:
            say(f"         {line}")

# --------------------------------------------------------------------------- #
# 6. Optional bits
# --------------------------------------------------------------------------- #
say("\n6 · Optional")
if os.environ.get("GROQ_API_KEY"):
    ok("GROQ_API_KEY set", "AI engine available (value not printed)")
else:
    say("  [ .. ] GROQ_API_KEY not set — fine: the offline engine needs no key.")
    say("         to enable the AI report: https://console.groq.com/keys")

try:
    result = subprocess.run(["streamlit", "--version"], capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        ok("streamlit CLI", result.stdout.strip() or result.stderr.strip())
    else:
        warn("streamlit CLI returned an error", (result.stderr or "").strip()[:120],
             "pip install --upgrade streamlit")
except Exception as exc:
    warn("streamlit CLI not found on PATH", str(exc),
         "python -m streamlit run app.py   (use the module form)")

tests_dir = ROOT / "tests"
if tests_dir.exists():
    say("  [ .. ] run the test suite with:  python -m pytest -q   (expect 38 passed)")

probe = ROOT / ".doctor_write_test"
try:
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    ok("folder is writable")
except Exception as exc:
    bad("folder not writable", str(exc), "move the project somewhere you own (e.g. ~/projects)")

# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
say("\n" + "=" * 78)
if problems:
    say(f"VERDICT: {len(problems)} problem(s) — fix the [FAIL] lines above, then re-run.")
    say("Paste this whole output into an assistant if you want help fixing them.")
else:
    say("VERDICT: ready. Start the app with:")
    say("    streamlit run app.py        (or: python -m streamlit run app.py)")
    say("Then open http://localhost:8501 and press 'Run Facebook Analysis'.")
say("=" * 78)

try:
    (ROOT / "doctor_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nSaved a copy to doctor_report.txt")
except Exception:
    pass

sys.exit(1 if problems else 0)
