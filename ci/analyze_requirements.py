import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result
from project_config import auto_record, cfg as _cfg, group_scenarios_by_requirement, rollup_kane_status

# On GitHub Actions: /home/runner/.testmuai/kaneai/sessions/
# On Windows local:  C:/Users/<user>/.testmuai/kaneai/sessions/
KANE_SESSIONS_DIR = Path.home() / ".testmuai" / "kaneai" / "sessions"
_KANE_PROJECT_CONFIGURED = False


def _parse_file_url(raw: str) -> str:
    """Convert a file:// URL (from Kane's CodeExport link) to an OS path.

    Handles both Linux  (file:///home/runner/...) and Windows
    (file:///C:/Users/...) formats that appear in Kane CLI terminal output.
    """
    token = raw.strip()
    if not token.lower().startswith("file://"):
        return token  # already a plain path
    # Strip the scheme — leaves ///home/... or ///C:/...
    no_scheme = token[7:]           # e.g.  /home/runner/... or /C:/Users/...
    if sys.platform == "win32":
        # file:///C:/path → /C:/path → strip leading slash → C:/path
        if no_scheme.startswith("/") and len(no_scheme) > 2 and no_scheme[2] == ":":
            no_scheme = no_scheme[1:]
    return no_scheme


def _resolve_code_export_path(raw_path: str) -> str:
    """Given a path that may point to a file or a directory, return the
    parent code-export directory only if it contains .py files."""
    p = Path(raw_path)
    # If it's already a directory, use it directly
    candidates = [p, p.parent]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.py")):
            return str(c)
    return ""


def _find_code_export_by_session_id(session_id: str) -> str:
    """Construct and verify the code-export path from a known Kane session ID.

    This is the authoritative lookup on GitHub Actions where session IDs are
    available via NDJSON and the sessions directory is at a fixed location.
    The path is deterministic: KANE_SESSIONS_DIR/<session_id>/code-export/
    """
    if not session_id:
        return ""
    candidate = KANE_SESSIONS_DIR / session_id / "code-export"
    if candidate.is_dir() and any(candidate.glob("*.py")):
        return str(candidate)
    return ""


def _kane_exe():
    """Return the kane-cli executable, resolving .cmd wrapper on Windows."""
    exe = shutil.which("kane-cli")
    if exe is None and sys.platform == "win32":
        exe = shutil.which("kane-cli.cmd")
    return exe or "kane-cli"


KANE_EXE = _kane_exe()

TARGET_URL = os.environ.get("TARGET_URL") or _cfg("target_url", "")


def _configure_kane_project():
    """Configure Kane CLI Test Manager project and folder once per process."""
    global _KANE_PROJECT_CONFIGURED
    if _KANE_PROJECT_CONFIGURED:
        return
    project_id = os.environ.get("KANE_PROJECT_ID", "")
    folder_id = os.environ.get("KANE_FOLDER_ID", "")
    if project_id:
        subprocess.run([KANE_EXE, "config", "project", project_id],
                       capture_output=True, text=True, check=False)
        print(f"[Stage 1] Kane project configured: {project_id}")
    if folder_id:
        subprocess.run([KANE_EXE, "config", "folder", folder_id],
                       capture_output=True, text=True, check=False)
        print(f"[Stage 1] Kane folder configured: {folder_id}")
    _KANE_PROJECT_CONFIGURED = True




def build_name():
    """Consistent build label shared by KaneAI and Playwright sessions in the same run."""
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"Agentic STLC #{run_number} | {today}" if run_number else f"Agentic STLC | {today}"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", default="requirements")
    parser.add_argument("--output", default="requirements/analyzed_requirements.json")
    parser.add_argument("--kane-results", default="reports/kane_results.json")
    parser.add_argument("--skip-kane", action="store_true")
    parser.add_argument("--demo-mode", action="store_true",
                        help="Load pre-generated results from ci/demo_kane_results.json instead of calling Kane")
    return parser.parse_args()


# Optional customer label in front of an AC line, e.g. "[AC-02] User can ..."
# or "[BR-1.1,BR-1.5] ..." (comma-separated, no spaces). Captured as `brd_ref`;
# the pipeline's own id stays AC-nnn.
_BRD_REF_RE = re.compile(r"^\[([A-Za-z0-9.,_-]+)\]\s*")


def _split_brd_ref(line: str) -> dict:
    m = _BRD_REF_RE.match(line)
    if m:
        return {"description": line[m.end():].strip(), "brd_ref": m.group(1)}
    return {"description": line.strip(), "brd_ref": None}


def extract_acceptance_criteria(text):
    """Extracts acceptance criteria using deterministic line parsing.

    Returns a list of {"description": str, "brd_ref": str | None}. A leading
    `[REF]` token on the line is stripped into `brd_ref`. Callers that only
    want the text should use extract_acceptance_criteria_texts()."""
    criteria = []
    lines = [line.strip() for line in text.splitlines()]
    capture = False
    for line in lines:
        if line.lower().strip().rstrip(":").startswith("acceptance criteria"):
            capture = True
            continue
        if capture:
            if not line or line.startswith("---") or line.lower().startswith("title") or \
               any(line.lower().startswith(p) for p in ["as a ", "i want to ", "so that ", "acceptance criteria"]):
                capture = False
                continue
            criteria.append(line)
    return [_split_brd_ref(c) for c in criteria if c.strip()]


def extract_acceptance_criteria_texts(text):
    """Compat wrapper — the pre-many-to-one return shape (plain strings,
    with any `[REF]` prefix removed)."""
    return [c["description"] for c in extract_acceptance_criteria(text)]


def make_title(description):
    words = description.replace(".", "").replace(":", "").split()
    return " ".join(words[:10]).strip().capitalize()


# Optional per-AC objective overrides. Empty by default — every AC
# falls through to its description verbatim. Add entries here ONLY when an AC's
# raw description doesn't give Kane enough flow guidance to plan a stable
# recording. The replay-first model means an override only fires on the first
# record; subsequent runs replay the saved _test.md regardless.
_KANE_TASK_OVERRIDES: dict[str, str] = {}


def _get_kane_task(description: str) -> str:
    """Return an optimized Kane task or the generic fallback."""
    dl = description.lower()
    for keyword, task in _KANE_TASK_OVERRIDES.items():
        if keyword in dl:
            return task
    return f"On {TARGET_URL} — {description}"


EXIT_STATUS = {0: "passed", 1: "failed", 2: "error", 3: "timeout"}


def _run_kane_indexed(args):
    return run_kane(*args)


def run_kane(index, description):
    username = os.environ.get("LT_USERNAME", "")
    access_key = os.environ.get("LT_ACCESS_KEY", "")
    if not username or not access_key:
        return {
            "status": "skipped",
            "summary": "Skipped Kane run: LT credentials not available.",
            "one_liner": "",
            "steps": [],
            "final_state": {},
            "duration": None,
            "test_url": "",
        }

    playwright_version = ""
    try:
        result = subprocess.run(
            ["playwright", "--version"], capture_output=True, text=True, check=False
        )
        parts = result.stdout.strip().split()
        playwright_version = parts[1] if len(parts) >= 2 else ""
    except Exception:
        pass

    session_name = f"AC-{index:03d} | {description[:80].strip()}"

    caps = {
        "browserName": "Chrome",
        "browserVersion": "latest",
        "LT:Options": {
            "platform": "Windows 10",
            "build": build_name(),
            "name": session_name,
            "user": username,
            "accessKey": access_key,
            "network": True,
            "video": True,
            "console": True,
            "tunnel": False,
            "tunnelName": "",
            "playwrightClientVersion": playwright_version,
        },
    }
    ws_endpoint = (
        "wss://cdp.lambdatest.com/playwright?capabilities="
        + urllib.parse.quote(json.dumps(caps))
    )
    task = _get_kane_task(description)
    command = [
        KANE_EXE, "run", task,
        "--username", username,
        "--access-key", access_key,
        "--ws-endpoint", ws_endpoint,
        "--agent",
        "--headless",
        "--timeout", "120",
        "--max-steps", "20",
        "--code-export",
        "--code-language", "python",
        "--skip-code-validation",
    ]
    run_start = time.time()
    completed = subprocess.run(command, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")

    exit_status = EXIT_STATUS.get(completed.returncode, "error")

    run_end = None
    step_summaries = []
    session_id = ""
    code_export_dir = ""
    combined = completed.stdout + "\n" + completed.stderr

    # ── Parse Kane NDJSON + plain-text output ──────────────────────────────
    # Kane CLI emits two kinds of output on stdout/stderr:
    #   1. NDJSON events  — one JSON object per line (step_end, run_end, …)
    #   2. Plain-text lines — the "links box" at session exit, e.g.:
    #        │  CodeExport   file:///home/runner/.testmuai/kaneai/sessions/UUID/code-export/  │
    #      or (without box borders):
    #        CodeExport  file:///home/runner/.testmuai/kaneai/sessions/UUID/code-export/
    #
    # Strategy:
    #   a) Try JSON parse first on every line.
    #   b) For non-JSON lines, scan for a "file://" token adjacent to "CodeExport".
    #   c) Also scan non-JSON lines for a bare UUID-shaped path segment that
    #      looks like a session directory path — this catches cases where Kane
    #      prints the path without the file:// scheme.
    # ────────────────────────────────────────────────────────────────────────
    import re as _re
    _UUID_RE = _re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        _re.IGNORECASE,
    )

    for raw in combined.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue

        # ── Attempt JSON parse ─────────────────────────────────────────────
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            event = None

        if event is not None:
            event_type = event.get("type", "")
            if event_type in ("step_end", "stepEnd") and event.get("summary"):
                step_summaries.append(event["summary"])
            elif event_type in ("run_end", "runEnd"):
                run_end = event
                # session_id may be on run_end directly, or nested under data/metadata
                session_id = (
                    event.get("session_id")
                    or event.get("sessionId")
                    or event.get("data", {}).get("session_id", "")
                    or ""
                )
            # Some Kane versions emit a dedicated code_export event
            elif event_type in ("code_export", "codeExport"):
                raw_path = event.get("path") or event.get("directory") or ""
                if raw_path:
                    code_export_dir = _resolve_code_export_path(raw_path)
            # session_id can also appear on non-run_end events (e.g. session_start)
            if not session_id:
                session_id = (
                    event.get("session_id")
                    or event.get("sessionId")
                    or ""
                )
            continue

        # ── Plain-text line: look for CodeExport + file:// ─────────────────
        upper = stripped.upper()
        if "CODEEXPORT" in upper.replace(" ", "").replace("-", ""):
            # Extract any file:// token on this line
            for token in stripped.split():
                if token.lower().startswith("file://"):
                    path = _parse_file_url(token)
                    resolved = _resolve_code_export_path(path)
                    if resolved:
                        code_export_dir = resolved
                        break
            # Also try bare path (no file:// scheme) — e.g. /home/runner/...
            if not code_export_dir:
                for token in stripped.split():
                    if "code-export" in token.lower() or "kaneai/sessions" in token.lower():
                        resolved = _resolve_code_export_path(token)
                        if resolved:
                            code_export_dir = resolved
                            break

        # ── Extract session UUID from any line that mentions sessions dir ──
        if not session_id and "sessions" in stripped.lower():
            m = _UUID_RE.search(stripped)
            if m:
                session_id = m.group(0)

    # ── Resolve code-export path ────────────────────────────────────────────
    # Priority:
    #   1. Explicit code_export event or CodeExport link already resolved above
    #   2. Deterministic session-ID lookup (GitHub Actions authoritative path)
    # We do NOT fall back to timestamp-based scanning because concurrent sessions
    # running in the ThreadPoolExecutor would produce ambiguous results.
    if not code_export_dir and session_id:
        code_export_dir = _find_code_export_by_session_id(session_id)

    if not run_end:
        raw_output = (completed.stdout + completed.stderr).strip()
        diagnostic = raw_output[:500] if raw_output else "Kane CLI produced no output."
        return {
            "status": exit_status,
            "summary": diagnostic,
            "one_liner": "",
            "steps": [],
            "final_state": {},
            "duration": None,
            "test_url": "",
            "session_id": session_id,
            "code_export_dir": code_export_dir,
        }

    return {
        "status": run_end.get("status", exit_status),
        "summary": run_end.get("summary", ""),
        "one_liner": run_end.get("one_liner", ""),
        "steps": step_summaries,
        "final_state": run_end.get("final_state", {}),
        "duration": run_end.get("duration"),
        "test_url": run_end.get("test_url", ""),
        "session_id": session_id,
        "code_export_dir": code_export_dir,
    }


def _empty_result(status: str, summary: str) -> dict:
    return {
        "status": status, "summary": summary,
        "one_liner": "", "steps": [], "final_state": {}, "duration": None, "test_url": "",
        "session_id": "", "code_export_dir": "", "asset_path": "", "replay_decision": "",
    }


def load_scenarios(path: Path) -> list:
    """scenarios.json → list of records ([] when absent or unparsable)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [sc for sc in data if isinstance(sc, dict)] if isinstance(data, list) else []


def _fan_out(requirements: list, grouped: dict, per_requirement_result) -> list:
    """Offline modes (demo / --skip-kane) have one result per requirement;
    copy it onto each of the requirement's scenarios so the rest of Stage 1
    sees the same per-scenario shape as a live dispatch."""
    results = []
    for req in requirements:
        base = per_requirement_result(req)
        for sc in grouped.get(req["id"], []):
            item = dict(base)
            item["scenario_id"] = sc["id"]
            item["requirement_id"] = req["id"]
            results.append(item)
    return results


def load_demo_results(requirements: list, grouped: dict) -> list:
    """Load pre-generated demo Kane results, mapped by requirement position
    (the demo file is positional) and fanned out to each scenario."""
    demo_path = Path("ci/demo_kane_results.json")
    if not demo_path.exists():
        raise FileNotFoundError(
            f"DEMO_MODE requires ci/demo_kane_results.json — file not found at {demo_path}"
        )
    demo_data = json.loads(demo_path.read_text(encoding="utf-8"))
    by_position = {req["id"]: i for i, req in enumerate(requirements)}

    def _for(req: dict) -> dict:
        i = by_position[req["id"]]
        if i < len(demo_data):
            return dict(demo_data[i])
        # No cached result for this requirement: report it honestly as not run.
        # Never synthesize a pass.
        return _empty_result("not_run", "CACHED run: no cached Kane result for this requirement — not run.")

    return _fan_out(requirements, grouped, _for)


def emit_metrics(stage, duration_seconds, cache_hit=False, criteria_count=0, scenario_count=0):
    """Append timing to pipeline_metrics.json — no-op if file absent."""
    metrics_path = Path("reports/pipeline_metrics.json")
    try:
        metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
        metrics.setdefault("stages", {})[stage] = {
            "duration_seconds": round(duration_seconds, 2),
            "cache_hit": cache_hit,
            "criteria_count": criteria_count,
            "scenario_count": scenario_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(metrics, indent=2))
    except Exception:
        pass


def _scenario_entry(kane: dict) -> dict:
    """Per-scenario block for analyzed_requirements.json (many-to-one)."""
    test_url = kane.get("test_url", "")
    return {
        "scenario_id": kane.get("scenario_id"),
        "kane_status": kane.get("status", "not_run"),
        "kane_links": [test_url] if test_url else [],
        "kane_case_link": kane.get("tms_case_url", ""),
        "kane_one_liner": kane.get("one_liner", ""),
        "kane_summary": kane.get("summary", ""),
        "kane_steps": kane.get("steps", []),
        "kane_final_state": kane.get("final_state", {}),
        "kane_duration": kane.get("duration"),
        "kane_asset_path": kane.get("asset_path", ""),
        "kane_code_export_dir": kane.get("code_export_dir", ""),
        "kane_replay_decision": kane.get("replay_decision", ""),
        "kane_session_id": kane.get("session_id", ""),
        "kane_drift_retries": kane.get("drift_retries", 0),
        "kane_drift_history": kane.get("drift_history", []),
    }


def build_requirement_record(req: dict, sc_results: list, today: str) -> dict:
    """Roll the scenario results up to one requirement record. Legacy
    single-value keys come from the first scenario (or are empty) so
    downstream consumers that predate many-to-one keep working."""
    entries = [_scenario_entry(k) for k in sc_results]
    first = entries[0] if entries else None
    links: list = []
    for e in entries:
        for link in e["kane_links"]:
            if link not in links:
                links.append(link)
    return {
        "id": req["id"],
        "brd_ref": req.get("brd_ref"),
        "title": make_title(req["description"]),
        "description": req["description"],
        "url": TARGET_URL,
        "kane_status": rollup_kane_status(e["kane_status"] for e in entries),
        "kane_one_liner": first["kane_one_liner"] if first else "",
        "kane_summary": first["kane_summary"] if first else "No scenarios cover this requirement.",
        "kane_steps": first["kane_steps"] if first else [],
        "kane_final_state": first["kane_final_state"] if first else {},
        "kane_duration": first["kane_duration"] if first else None,
        "kane_links": links,
        "kane_session_id": first["kane_session_id"] if first else "",
        "kane_code_export_dir": first["kane_code_export_dir"] if first else "",
        "kane_asset_path": first["kane_asset_path"] if first else "",
        "kane_replay_decision": first["kane_replay_decision"] if first else "",
        "kane_drift_retries": first["kane_drift_retries"] if first else 0,
        "kane_drift_history": first["kane_drift_history"] if first else [],
        "scenarios": entries,
        "last_analyzed": today,
    }


def main():
    args = parse_args()
    demo_mode = args.demo_mode or os.environ.get("DEMO_MODE", "false").lower() == "true"
    kane_mode = "cached" if demo_mode else ("not_run" if args.skip_kane else "live")

    Path("reports").mkdir(exist_ok=True)
    print_stage_header("1", "ANALYZE_REQUIREMENTS", "Parse requirements and run KaneAI functional verification")

    req_path = Path(args.requirements)
    parsed = []
    if req_path.is_dir():
        for req_file in sorted(req_path.glob("*.txt")):
            parsed.extend(extract_acceptance_criteria(req_file.read_text(encoding="utf-8")))
    else:
        parsed = extract_acceptance_criteria(req_path.read_text(encoding="utf-8"))
    requirements = [
        {"id": f"AC-{i:03d}", "description": c["description"], "brd_ref": c["brd_ref"]}
        for i, c in enumerate(parsed, start=1)
    ]

    scenarios_path = Path(os.environ.get("SCENARIOS_PATH", "scenarios/scenarios.json"))
    scenarios = load_scenarios(scenarios_path)
    grouped = group_scenarios_by_requirement(scenarios)
    active_count = sum(len(v) for v in grouped.values())
    uncovered = [r["id"] for r in requirements if not grouped.get(r["id"])]
    print(
        f"[Stage 1] {len(requirements)} requirements, {active_count} active scenarios "
        f"from {scenarios_path}, {len(uncovered)} uncovered"
        + (f": {', '.join(uncovered)}" if uncovered else "")
    )

    today = datetime.now(timezone.utc).date().isoformat()
    stage_start = time.time()

    if demo_mode:
        print("=" * 62)
        print(f"  [CACHED] DEMO_MODE — replaying cached Kane results for {len(requirements)} criteria")
        print("  [CACHED] No live Kane sessions run. Reports will be marked CACHED.")
        print("=" * 62)
        results = load_demo_results(requirements, grouped)
        cache_hit = True
    elif args.skip_kane:
        results = _fan_out(
            requirements, grouped,
            lambda _req: _empty_result("pending", "Kane run not attempted."),
        )
        cache_hit = False
    else:
        _configure_kane_project()
        # Replay-first dispatch, per scenario: the kane_dispatch module either
        # replays an existing _test.md asset (cheap, no LLM reasoning) or
        # records a new one (costs Kane tokens; blocked when
        # kaneai.auto_record is false). Decisions → reports/replay_decisions.json.
        from kane_dispatch import dispatch_all  # local import to keep top-level fast
        username = os.environ.get("LT_USERNAME", "")
        access_key = os.environ.get("LT_ACCESS_KEY", "")
        force_re = os.environ.get("FORCE_RE_AUTHOR", "false").lower() == "true"
        max_workers = int(os.environ.get("KANE_MAX_WORKERS", "5"))
        print(
            f"[Stage 1] Test.md dispatch — workers={max_workers}, {active_count} scenarios "
            f"across {len(requirements)} criteria, force_re_author={force_re}, "
            f"auto_record={auto_record()}"
        )
        results = dispatch_all(
            requirements, scenarios,
            username=username, access_key=access_key, max_workers=max_workers,
        )
        cache_hit = False

    results_by_req: dict = {}
    for kane in results:
        results_by_req.setdefault(kane.get("requirement_id"), []).append(kane)

    analyzed = []
    kane_results = []
    for req in requirements:
        item = build_requirement_record(req, results_by_req.get(req["id"], []), today)
        item["kane_mode"] = kane_mode
        analyzed.append(item)
        for kane in results_by_req.get(req["id"], []):
            test_url = kane.get("test_url", "")
            kane_results.append({
                "scenario_id": kane.get("scenario_id"),
                "requirement_id": req["id"],
                "brd_ref": req.get("brd_ref"),
                "title": item["title"],
                "status": kane.get("status", "not_run"),
                "one_liner": kane.get("one_liner", ""),
                "summary": kane.get("summary", ""),
                "steps": kane.get("steps", []),
                "final_state": kane.get("final_state", {}),
                "duration": kane.get("duration"),
                "link": test_url,
                "case_link": kane.get("tms_case_url", ""),
                "url": item["url"],
                "asset_path": kane.get("asset_path", ""),
                "replay_decision": kane.get("replay_decision", ""),
                "session_id": kane.get("session_id", ""),
                "code_export_dir": kane.get("code_export_dir", ""),
                "kane_mode": kane_mode,
            })

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(analyzed, indent=2) + "\n", encoding="utf-8")

    kane_path = Path(args.kane_results)
    kane_path.parent.mkdir(parents=True, exist_ok=True)
    kane_path.write_text(json.dumps(kane_results, indent=2) + "\n", encoding="utf-8")

    print(f"{'ID':8} {'Kane':<9} {'Scenarios':<18} {'Title':<40} {'Link'}")
    for item in analyzed:
        link = item["kane_links"][0] if item["kane_links"] else ""
        sc_ids = ", ".join(str(s["scenario_id"]) for s in item["scenarios"]) or "-"
        print(f"{item['id']:8} {item['kane_status']:<9} {sc_ids:18.18} {item['title']:40.40} {link}")

    elapsed = time.time() - stage_start
    mode_label = "CACHED" if demo_mode else "live"
    passed_count = sum(1 for a in analyzed if a["kane_status"] == "passed")
    failed_count = sum(1 for a in analyzed if a["kane_status"] == "failed")
    sc_passed = sum(1 for k in kane_results if k["status"] == "passed")

    print_stage_result("1", "ANALYZE_REQUIREMENTS", {
        "Requirements parsed":  len(analyzed),
        "Criteria analyzed":    f"{len(analyzed)} ({mode_label}, workers={os.environ.get('KANE_MAX_WORKERS', '5')})",
        "Scenarios dispatched": f"{len(kane_results)} ({sc_passed} passed)",
        "Uncovered":            len(uncovered),
        "Kane passed":          f"{passed_count}/{len(analyzed)}",
        "Kane failed":          failed_count,
        "Pass rate":            f"{round(passed_count / len(analyzed) * 100, 1) if analyzed else 0}%",
        "Duration":             f"{elapsed:.1f}s",
        "Output":               args.output,
    })
    emit_metrics("stage1_kane", elapsed, cache_hit=cache_hit,
                 criteria_count=len(requirements), scenario_count=len(kane_results))


if __name__ == "__main__":
    main()
