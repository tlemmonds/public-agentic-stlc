"""Kane replay runner — executes a saved _test.md asset against the AUT.

Replay is the "fast path" of the new architecture: Kane re-walks the recorded
steps without re-planning, so each run consumes very few tokens. Token cost
is only paid during recording (kane_record.py).

Returns the same shape as analyze_requirements.py's run_kane(), so it can
be a drop-in replacement at the dispatch layer.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import project_config  # noqa: E402  (kaneai.adaptive_heal)

# Mirror of the EXIT_STATUS map in analyze_requirements.py — kept local so
# this module stays self-contained.
EXIT_STATUS = {0: "passed", 1: "failed", 2: "error", 3: "timeout"}

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _kane_exe() -> str:
    """Resolve the kane-cli launcher (Windows ships the .cmd shim)."""
    exe = shutil.which("kane-cli")
    if exe is None and sys.platform == "win32":
        exe = shutil.which("kane-cli.cmd")
    return exe or "kane-cli"


def _build_caps(session_name: str, build_name: str, *, username: str, access_key: str) -> str:
    """Build the LambdaTest Playwright caps URL — same pattern as
    analyze_requirements.run_kane()."""
    playwright_version = ""
    try:
        result = subprocess.run(
            ["playwright", "--version"], capture_output=True, text=True, check=False
        )
        parts = result.stdout.strip().split()
        playwright_version = parts[1] if len(parts) >= 2 else ""
    except Exception:
        pass

    caps = {
        "browserName": "Chrome",
        "browserVersion": "latest",
        "LT:Options": {
            "platform": "Windows 10",
            "build": build_name,
            "name": session_name,
            "user": username,
            "accessKey": access_key,
            "network": True,
            "video": True,
            "console": True,
            "tunnel": False,
            "tunnelName": "",
            "playwrightClientVersion": playwright_version,
            # Kane pauses to reason between actions; the grid default idle
            # timeout closed long sessions mid-run (SC-002/SC-004 records).
            "idleTimeout": int(os.environ.get("LT_IDLE_TIMEOUT", "300")),
        },
    }
    return (
        "wss://cdp.lambdatest.com/playwright?capabilities="
        + urllib.parse.quote(json.dumps(caps))
    )


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def _frontmatter_int(asset_path: Path, key: str) -> int | None:
    """Read an integer key (max_steps, timeout) from the asset's YAML frontmatter, if any."""
    try:
        m = _FM_RE.match(asset_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not m:
        return None
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip() == key:
            try:
                return int(v.strip())
            except ValueError:
                return None
    return None


def _effective_timeout(asset_path: Path, pipeline_timeout: int) -> int:
    """Never cut an asset shorter than its own declared timeout."""
    declared = _frontmatter_int(asset_path, "timeout")
    return max(pipeline_timeout, declared or 0)


def _clear_replay_cache(asset_path: Path) -> None:
    """Empty the asset's sibling `output-<stem>/` before a replay, keeping only
    `.internal/meta.json` (the Test Manager link). `kane-cli testmd run` writes the
    steps it executed there and the next replay replays THAT list instead of the
    recording: a cache written by an earlier replay can miss actions (SC-005's had
    only "Navigate" and the check), so every check then reads the wrong page."""
    out = asset_path.parent / f"output-{_asset_stem(asset_path)}"
    if not out.is_dir():
        return
    for entry in out.iterdir():
        if entry.name == ".internal":
            for inner in entry.iterdir():
                if inner.name != "meta.json":
                    shutil.rmtree(inner) if inner.is_dir() else inner.unlink()
        else:
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()


def _recorded_start_url(asset_path: Path) -> str | None:
    """The recorded start_url value of an asset that uses {{start_url}}, else None."""
    try:
        text = asset_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "start_url" not in text:
        return None
    m = re.search(r"start_url:\s*\n\s*value:\s*(?:>-?\s*\n\s*)?(https?://\S+)", text)
    return m.group(1) if m else None


def replay(
    asset_path: Path,
    *,
    session_name: str,
    build_name: str,
    username: str,
    access_key: str,
    timeout_seconds: int = 180,
    variables_file: Path | None = None,
    code_export_dir: Path | None = None,
) -> dict[str, Any]:
    """Replay a saved _test.md and return a dict matching run_kane()'s shape.

    code_export_dir: when provided, Kane writes a Python Playwright export
    into that directory. Used by Stage 3 to derive the regression test.
    """
    if not asset_path.exists():
        return _make_skipped_result(f"asset not found: {asset_path}")

    if not username or not access_key:
        return _make_skipped_result("LT credentials not available")

    _clear_replay_cache(asset_path)
    ws_endpoint = _build_caps(session_name, build_name, username=username, access_key=access_key)
    cli = _kane_exe()
    # Kane CLI 0.3.1 split test.md replay onto its own subcommand:
    #   kane-cli testmd run <path>     ← replay
    #   kane-cli run "<objective>"     ← inline authoring (used by kane_record.py)
    command = [
        cli, "testmd", "run", str(asset_path),
        "--username", username,
        "--access-key", access_key,
        "--ws-endpoint", ws_endpoint,
        "--agent",
        "--headless",
        "--timeout", str(_effective_timeout(asset_path, timeout_seconds)),
    ]
    # kaneai.fresh_session_per_run: start the replay in a new, empty session.
    target = project_config.cfg("target_url") or ""
    if project_config.fresh_sessions() and target:
        start_url = project_config.fresh_session_url(os.environ.get("TARGET_URL") or target)
        command += ["--url", start_url]
    # Assets recorded with "On {{start_url}} — " navigate to the --variables
    # value; without it the browser stays on about:blank. Pass the session the
    # asset was recorded in, not a fresh one: kane-cli saves the value to the shared Test
    # Manager global, which must keep pointing at a known, recorded session
    # ("Execute and verify" runs there).
    recorded = _recorded_start_url(asset_path)
    if recorded and project_config.start_url_variable() and variables_file is None:
        command += ["--variables", json.dumps({"start_url": {"value": recorded}})]
    # Ingested assets declare their own step budget in frontmatter (everdemo:
    # 30–80). Only impose the pipeline default when the asset is silent.
    if _frontmatter_int(asset_path, "max_steps") is None:
        command += ["--max-steps", os.environ.get("KANE_REPLAY_MAX_STEPS", "30")]
    command += [
        # Note: --retry and --on-lock-conflict were removed because Kane 0.3.1
        # on Windows rejects them with "--retry requires basic auth credentials
        # for the lock API" even when --username/--access-key are passed
        # inline. Pipeline-level retry happens via re-record on hash drift, and
        # each worker writes to its own per-sc output dir so lock collisions
        # don't occur in practice.
    ]
    if variables_file is not None and variables_file.exists():
        command.extend(["--variables-file", str(variables_file)])
    if code_export_dir is not None:
        code_export_dir.mkdir(parents=True, exist_ok=True)
        command.extend(["--code-export", "--code-language", "python", "--skip-code-validation"])
    # A replay miss is a finding, not something to heal silently — unless the
    # project opts in via `kaneai.adaptive_heal: true`.
    if not project_config.adaptive_heal():
        command.append("--no-adaptive-heal")

    started = time.time()
    # Subprocess-level timeout = Kane's --timeout + a 60s safety margin so
    # a hung CLI doesn't pin a worker thread forever (the smoke-test on
    # 2026-05-15 ran for an hour with --timeout=180 set on Kane itself,
    # confirming Kane can ignore its own timeout flag).
    #
    # cwd=REPO_ROOT so kane-cli auto-loads `{cwd}/.testmuai/variables/*.json`
    # and resolves the asset's sibling `output-<stem>/` sidecar (TMS reuse).
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
            timeout=_effective_timeout(asset_path, timeout_seconds) + 60,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "summary": f"kane-cli testmd run wedged past {timeout_seconds + 60}s — killed by subprocess.TimeoutExpired",
            "one_liner": "", "steps": [], "final_state": {},
            "duration": round(time.time() - started, 2),
            "test_url": "", "session_id": "",
            "code_export_dir": str(code_export_dir) if code_export_dir else "",
        }
    duration = time.time() - started
    result = _parse_kane_output(completed, duration, code_export_dir)
    _harvest_code_export(
        [asset_path.parent / f"output-{_asset_stem(asset_path)}" / "playwright-python-code"], code_export_dir)
    # Evidence links: the Automate session Kane drove (video/logs) and the TMS case
    # the asset belongs to. Neither is in kane-cli's NDJSON, so resolve them here.
    result["lt_build_name"] = build_name
    result["lt_session_name"] = session_name
    result["tms_case_url"] = _tms_case_url(asset_path)
    if not result.get("test_url"):
        result.update({k: v for k, v in
                       _resolve_automate_session(build_name, session_name, started, username, access_key).items()
                       if v})
    return result


# Kane CLI 0.9 writes its code export beside the session's _test.md, under
# `{cwd}/.testmuai/tests/output-<name>/playwright-python-code/` (record) or
# `<asset dir>/output-<stem>/playwright-python-code/` (testmd replay), not into
# the --code-export dir we pass. Copy the runnable files into that dir; never
# the .env templates.
_EXPORT_FILES = ("test.py", "requirements.txt")


def _harvest_code_export(candidates: list[Path], dest: Path | None) -> bool:
    if dest is None:
        return False
    if any(dest.glob("*.py")):
        return True
    for src in candidates:
        if (src / "test.py").is_file():
            dest.mkdir(parents=True, exist_ok=True)
            for name in _EXPORT_FILES:
                if (src / name).is_file():
                    shutil.copyfile(src / name, dest / name)
            return True
    return False


_AUTOMATE_API = "https://api.lambdatest.com/automation/api/v1"
_TMS_UI = "https://test-manager.lambdatest.com"


def _asset_stem(asset_path: Path) -> str:
    name = asset_path.name
    return name[: -len("_test.md")] if name.endswith("_test.md") else asset_path.stem


def _tms_case_url(asset_path: Path) -> str:
    """Test Manager case page for an ingested asset, from its `output-<stem>/.internal/meta.json`
    sidecar (project_id + testcase_id). Empty when the asset has no sidecar (pipeline-recorded)."""
    meta = asset_path.parent / f"output-{_asset_stem(asset_path)}" / ".internal" / "meta.json"
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    project_id, case_id = m.get("project_id", ""), m.get("testcase_id", "")
    return f"{_TMS_UI}/projects/{project_id}/test-cases/{case_id}" if project_id and case_id else ""


def _lt_get(url: str, username: str, access_key: str, timeout: int = 20) -> dict:
    import base64
    import urllib.request
    token = base64.b64encode(f"{username}:{access_key}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_automate_session(build_name: str, session_name: str, started_epoch: float,
                              username: str, access_key: str) -> dict[str, str]:
    """Find the LambdaTest Automate session a replay ran in (Kane drives the cloud
    browser through --ws-endpoint, so every replay is a real Automate session with
    video + logs). kane-cli's NDJSON never reports the test id, so look it up by
    build name + session name, newest session started after `started_epoch`.
    Returns {} on any API problem — links are a nicety, never a gate."""
    if not (username and access_key and build_name and session_name):
        return {}
    try:
        builds = _lt_get(f"{_AUTOMATE_API}/builds?limit=40", username, access_key).get("data", [])
        build_ids = sorted((b["build_id"] for b in builds if b.get("name") == build_name), reverse=True)
        floor = started_epoch - 120
        best: dict = {}
        for build_id in build_ids[:3]:
            offset = 0
            while True:
                page = _lt_get(f"{_AUTOMATE_API}/sessions?build_id={build_id}&limit=100&offset={offset}",
                               username, access_key)
                rows = page.get("data", [])
                for s in rows:
                    if (s.get("name") or "").strip() != session_name.strip():
                        continue
                    try:
                        created = datetime.strptime(s.get("create_timestamp", ""), "%Y-%m-%d %H:%M:%S") \
                            .replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        created = 0
                    if created >= floor and created >= best.get("_created", -1):
                        best = {**s, "_created": created}
                total = ((page.get("Meta") or page.get("meta") or {}).get("result_set") or {}).get("total", 0)
                offset += len(rows)
                if not rows or offset >= total:
                    break
            if best:
                break
        if not best:
            return {}
        test_id = best.get("test_id") or best.get("session_id") or ""
        return {
            "test_url": f"https://automation.lambdatest.com/test?testID={test_id}" if test_id else "",
            "lt_test_id": test_id,
            "video_url": best.get("video_url") or "",
        }
    except Exception as exc:  # noqa: BLE001 — never fail a replay over a link lookup
        print(f"    [kane_replay] automate session lookup skipped: {exc}", file=sys.stderr)
        return {}


def _make_skipped_result(message: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "summary": message,
        "one_liner": "",
        "steps": [],
        "final_state": {},
        "duration": None,
        "test_url": "",
        "session_id": "",
        "code_export_dir": "",
    }


def _parse_kane_output(completed: subprocess.CompletedProcess, duration: float, code_export_dir: Path | None) -> dict[str, Any]:
    """Parse Kane's NDJSON + plain-text output into the run_kane() result
    shape. This is a deliberate copy of the parsing logic in
    analyze_requirements.run_kane() so kane_replay can be a drop-in
    substitute without importing back into that module."""
    exit_status = EXIT_STATUS.get(completed.returncode, "error")
    combined = completed.stdout + "\n" + completed.stderr

    run_end: dict | None = None          # last run_end (one per test.md section)
    run_ends: list[dict] = []
    step_summaries: list[str] = []       # every action: "<kind>: <text>"
    sections: list[dict] = []            # test.md sections: heading, status, duration_s
    md_summary: dict | None = None       # test_md_summary (commit / heal / decisions)
    md_done: dict | None = None          # test_md_done (overall_status, wall-clock, session)
    session_id = ""
    discovered_export_dir = ""
    assertions_passed = 0
    assertions_failed = 0

    for raw in combined.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            event = None

        if event is not None:
            event_type = event.get("type", "")
            if event_type in ("step_end", "stepEnd"):
                if event.get("summary"):
                    step_summaries.append(event["summary"])
                if event.get("kind") == "assert":
                    if event.get("status") == "passed":
                        assertions_passed += 1
                    else:
                        assertions_failed += 1
            elif event_type == "test_md_step_start":
                sections.append({"index": event.get("step_index"), "heading": event.get("heading") or "",
                                 "status": "", "duration_s": None})
            elif event_type == "test_md_step_end":
                idx = event.get("step_index")
                for sec in reversed(sections):
                    if sec["index"] == idx:
                        sec["status"] = event.get("status", "")
                        sec["duration_s"] = event.get("duration_s")
                        break
            elif event_type == "test_md_summary":
                md_summary = event
            elif event_type == "test_md_done":
                md_done = event
            elif event_type in ("run_end", "runEnd"):
                run_end = event
                run_ends.append(event)
                session_id = (
                    event.get("session_id")
                    or event.get("sessionId")
                    or event.get("data", {}).get("session_id", "")
                    or ""
                )
            elif event_type in ("code_export", "codeExport"):
                raw_path = event.get("path") or event.get("directory") or ""
                if raw_path:
                    discovered_export_dir = raw_path
            if not session_id:
                session_id = event.get("session_id") or event.get("sessionId") or ""
            continue

        if not session_id and "sessions" in stripped.lower():
            m = _UUID_RE.search(stripped)
            if m:
                session_id = m.group(0)

    export_dir = discovered_export_dir or (str(code_export_dir) if code_export_dir else "")

    if not run_end:
        diagnostic = combined.strip()[:500] or "Kane CLI produced no output."
        return {
            "status": exit_status,
            "summary": diagnostic,
            "one_liner": "",
            "steps": step_summaries,
            "sections": sections,
            "final_state": {},
            "duration": round(duration, 2),
            "test_url": "",
            "session_id": session_id,
            "code_export_dir": export_dir,
        }

    # Overall status: the test.md verdict outranks the last section's run_end.
    status = (md_done or {}).get("overall_status") or run_end.get("status", exit_status)
    # Wall-clock: test_md_done.duration_s is the whole replay; run_end.duration is one section.
    wall = (md_done or {}).get("duration_s") or round(duration, 2)

    # Kane writes prose (summary / one_liner) only when it has something to say —
    # a failure verdict, a heal, a bug report. A clean deterministic replay
    # emits summary "" on every section, so synthesise the observation from
    # what it did emit; keep Kane's own words whenever they exist.
    kane_summary = (run_end.get("summary") or "").strip()
    kane_one_liner = (run_end.get("one_liner") or "").strip()
    if not kane_summary or not kane_one_liner:
        syn_summary, syn_one_liner = _synthesize_observation(
            status=status, sections=sections, run_ends=run_ends, md_summary=md_summary,
            wall_s=wall, assertions_passed=assertions_passed, assertions_failed=assertions_failed,
            actions=len(step_summaries),
        )
        kane_summary = kane_summary or syn_summary
        kane_one_liner = kane_one_liner or syn_one_liner

    return {
        "status": status,
        "summary": kane_summary,
        "one_liner": kane_one_liner,
        "steps": step_summaries,
        "sections": sections,
        "assertions": {"passed": assertions_passed, "failed": assertions_failed},
        "final_state": run_end.get("final_state", {}),
        "final_url": run_end.get("final_url", ""),
        "duration": wall,
        "test_url": run_end.get("test_url", ""),
        "session_id": session_id,
        "code_export_dir": export_dir,
    }


def _synthesize_observation(*, status: str, sections: list[dict], run_ends: list[dict], md_summary: dict | None,
                            wall_s: Any, assertions_passed: int, assertions_failed: int, actions: int) -> tuple[str, str]:
    """Build (summary, one_liner) from Kane's structured events for runs where it wrote no prose."""
    total = len(sections)
    passed = sum(1 for s in sections if s.get("status") == "passed")
    failed = [s for s in sections if s.get("status") and s.get("status") != "passed"]
    final_url = (run_ends[-1].get("final_url") or "") if run_ends else ""
    steps_word = f"{passed}/{total} sections" if total else f"{actions} actions"
    decisions = ((md_summary or {}).get("steps") or {}) if isinstance((md_summary or {}).get("steps"), dict) else {}
    replayed = decisions.get("replay_decisions")
    authored = decisions.get("author_decisions")
    mode = ""
    if replayed is not None and authored is not None:
        mode = "deterministic replay of the recorded actions" if not authored else f"{authored} section(s) re-authored, {replayed} replayed"
    asserts = f"{assertions_passed} assertion(s) passed" + (f", {assertions_failed} failed" if assertions_failed else "")

    if status == "passed":
        one_liner = f"Replayed {steps_word} in {wall_s}s — {asserts}" + (f"; ended on {final_url}" if final_url else "")
    else:
        first_fail = failed[0]["heading"] if failed else "unknown section"
        one_liner = f"{status.capitalize()} at “{first_fail}” — {passed}/{total} sections passed, {asserts}"

    lines = [one_liner + (f" ({mode})." if mode else ".")]
    for s in sections:
        mark = "✓" if s.get("status") == "passed" else ("✗" if s.get("status") else "…")
        dur = f" ({s['duration_s']}s)" if s.get("duration_s") is not None else ""
        heading = s.get("heading") or f"section {s.get('index')}"
        lines.append(f"{mark} {heading}{dur}")
    return "\n".join(lines), one_liner


def main() -> int:
    """CLI: replay a single _test.md and print the parsed result."""
    if len(sys.argv) < 2:
        print("usage: kane_replay.py <path/to/foo_test.md>", file=sys.stderr)
        return 2
    asset = Path(sys.argv[1]).resolve()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    build_name = f"Agentic STLC #{run_number} | {today}" if run_number else f"Agentic STLC | {today}"

    result = replay(
        asset,
        session_name=f"Replay | {asset.name}",
        build_name=build_name,
        username=os.environ.get("LT_USERNAME", ""),
        access_key=os.environ.get("LT_ACCESS_KEY", ""),
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
