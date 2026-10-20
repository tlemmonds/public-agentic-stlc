"""Shared stage result printing utility for Agentic STLC pipeline."""

import os
import sys
from datetime import datetime, timezone

# urllib on python.org / Homebrew macOS builds has no CA bundle, so LambdaTest
# API lookups fail with CERTIFICATE_VERIFY_FAILED. Point OpenSSL at certifi's
# bundle (installed with httpx) unless the environment already chose one. Child
# processes inherit it.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass


def print_stage_result(
    stage_num: str,
    stage_name: str,
    details: dict,
    success: bool = True,
) -> None:
    """Print structured stage completion summary to stdout."""
    status = "complete" if success else "FAILED"
    icon = "✅" if success else "❌"
    width = 62
    print(f"\n{'=' * width}")
    print(f"  {icon}  [Stage {stage_num}] {stage_name} — {status}")
    print(f"{'=' * width}")
    for key, value in details.items():
        print(f"  {key:<26}: {value}")
    print(f"  {'Completed at':<26}: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"{'=' * width}\n")
    sys.stdout.flush()


def print_stage_header(stage_num: str, stage_name: str, description: str = "") -> None:
    """Print stage start banner."""
    width = 62
    print(f"\n{'─' * width}")
    print(f"  ▶  [Stage {stage_num}] {stage_name}")
    if description:
        print(f"     {description}")
    print(f"{'─' * width}")
    sys.stdout.flush()


# ── Run validity (CACHED / HyperExecute-skipped markers) ──────────────────────
# ci/agent.py writes reports/run_validity.json after Stage 5; every report
# writer prepends run_banner_lines() so a cached or partial run can never be
# mistaken for a live one.
import json as _json
from pathlib import Path as _Path

RUN_VALIDITY_PATH = _Path("reports/run_validity.json")


def load_run_validity() -> dict:
    try:
        return _json.loads(RUN_VALIDITY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current_release(release_dir: str = "release_notes") -> str | None:
    """The newest release with a lock file (e.g. "v2.0.0"): the release a run
    verifies and archives under."""
    import re as _re
    from pathlib import Path as _Path

    def key(p):
        m = _re.match(r"v(\d+)\.(\d+)\.(\d+)\.lock\.json$", p.name)
        return tuple(int(x) for x in m.groups()) if m else (-1,)
    locks = sorted(_Path(release_dir).glob("v*.lock.json"), key=key)
    return locks[-1].name[: -len(".lock.json")] if locks else None


def run_banner_lines(validity: dict | None = None) -> list[str]:
    """Markdown warning lines for the top of a report ([] for a valid live run)."""
    v = load_run_validity() if validity is None else validity
    lines: list[str] = []
    if v.get("hyperexecute") == "skipped":
        lines.append(f"> ⛔ **HyperExecute skipped — not a valid run.** {v.get('hyperexecute_reason', '')}".rstrip())
    if v.get("kane_mode") == "cached":
        lines.append("> 🗄️ **CACHED** — Kane results replayed from cache (DEMO_MODE or CI cache hit); no live Kane sessions ran.")
    if not v:
        lines.append("> ⚠️ Run validity unknown — reports/run_validity.json missing (was ci/agent.py run?).")
    return lines + [""] if lines else []


def redact(text: str) -> str:
    """Mask the LambdaTest access key (raw or URL-encoded) in text bound for logs."""
    import urllib.parse
    key = os.environ.get("LT_ACCESS_KEY", "")
    if len(key) >= 6:
        text = text.replace(key, "***").replace(urllib.parse.quote(key, safe=""), "***")
    return text
