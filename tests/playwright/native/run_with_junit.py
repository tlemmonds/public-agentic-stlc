"""Run one staged testmu Playwright export and emit the framework's result artifacts.

Framework-owned. Stage 3a (ci/collect_kane_exports.py) stages every
`export_kind == "testmu"` scenario verbatim into tests/playwright/native/<sc_id_lower>/
(test.py + requirements.txt). Those exports are generated code — `kane-cli testmd
export` rewrites test.py wholesale — and they use the async `testmu` SDK, so they
can't be embedded into the sync conftest. The reporting shim therefore lives here,
one level above the staged folders, where a re-export cannot clobber it.

Contract (ci/run_selected.py does `cd tests/playwright/native/<sc_id_lower>` first):
  cwd            tests/playwright/native/<sc_id_lower>   (sc_013 → SC-013)
  runs           python test.py   with TESTMU_HEADLESS=true (existing env respected).
                 With kaneai.fresh_session_per_run, a copy (.session_test.py) whose
                 page.goto() calls to target_url point at a new, empty /s/<id>/
                 session runs instead, with testmu_compat imported first (a "Yes"/"No"
                 answer equals a recorded "true"/"false"; kaneai.yes_no_compat).
                 Dropdown steps exported as a bare select_option() get the value from
                 their step label (kaneai.select_option_repair). test.py itself is
                 never modified.
  no assertion   an export with no verify_assertion / expect( is reported failed:
                 a test that checks nothing must not count as a pass
  writes         reports/native_<SC-ID>.xml               JUnit, testcase name = SC-ID, classname = "native"
                 reports/kane_result_<SC-ID>_chrome.json  same schema conftest.py writes, source = "native"
  exit code      the test's exit code (77 = skip sentinel from the collect-time stub → exit 0)

Both artifacts are written even when test.py crashes on import (e.g. `testmu` not
installed) so Stage 6 resolves the scenario to `failed` rather than `data_unavailable`.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

SKIP_EXIT_CODE = 77          # written by the collect-time stub when the export was missing
BROWSER = "chrome"           # testmu exports drive their own (chromium) session
LT_BROWSER = "Chrome"
OUTPUT_TAIL = 8000           # JUnit failure body — tail, the traceback is at the end
ERROR_TAIL = 500             # matches conftest's longrepr[:500]

HERE = Path(__file__).resolve().parent                # tests/playwright/native
REPO_ROOT = HERE.parent.parent.parent                 # repo root
REPORTS = REPO_ROOT / "reports"

_LINK_RE = re.compile(r"https?://[\w.-]*lambdatest\.com/[^\s'\"<>]+")


def _sc_id_from_folder(folder: str) -> str:
    """sc_013 → SC-013. Anything else is upper-cased with '_' → '-' as a best effort."""
    m = re.fullmatch(r"sc[_-](\d+)", folder, re.IGNORECASE)
    if m:
        return f"SC-{int(m.group(1)):03d}"
    return folder.upper().replace("_", "-")


def _build_name() -> str:
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    if run_number.startswith("${"):  # unexpanded hyperexecute.yaml placeholder (local submit)
        run_number = ""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"Agentic STLC #{run_number} | {today}" if run_number else f"Agentic STLC | {today}"


def _requirement_id(sc_id: str) -> str:
    """Best-effort lookup in scenarios/scenarios.json; '' when unavailable."""
    try:
        data = json.loads((REPO_ROOT / "scenarios" / "scenarios.json").read_text(encoding="utf-8"))
        for sc in data:
            if isinstance(sc, dict) and sc.get("id") == sc_id:
                return sc.get("requirement_id") or ""
    except Exception:
        pass
    return ""


_ASSERT_RE = re.compile(r"verify_assertion|expect\(")


def _config_value(key: str) -> str:
    """One scalar from agentic-stlc.config.yaml without a YAML dependency (the VM
    may not have PyYAML). Matches the first `key: value` line at any indent."""
    try:
        text = (REPO_ROOT / "agentic-stlc.config.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(rf"^\s*{re.escape(key)}:\s*['\"]?([^'\"#\s]+)", text, re.MULTILINE)
    return m.group(1) if m else ""


# Prepended to the per-run copy: puts tests/playwright/native on sys.path and
# installs testmu_compat (yes/no ↔ true/false) before the export imports testmu.
_COMPAT_HEADER = (
    "import sys as _s, os as _o; "
    "_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))); "
    "import testmu_compat  # noqa: E402,F401  (run_with_junit.py)\n"
)


# Kane CLI 0.9 exports a dropdown choice as `await el.select_option()` with no
# value, so the choice is lost (the option stays "— none —"). The value is in the
# step label: `testmu.step('Select "high" in "Priority"', …)`. Hand it back.
_SELECT_STEP_RE = re.compile(
    r"""(testmu\.step\(\s*'Select "(?P<value>[^"]*)" in "[^"]*"'.*?\n)"""
    r"""(?P<body>(?:[ \t]+.*\n|\s*\n)*?)"""
    r"""(?P<call>[ \t]+await \w+\.select_option\()\)""")


def _repair_select_options(src: str) -> tuple[str, int]:
    def _fix(m: re.Match) -> str:
        return f"{m.group(1)}{m.group('body')}{m.group('call')}label={m.group('value')!r})"
    return _SELECT_STEP_RE.subn(_fix, src)


def _session_copy(cwd: Path) -> str:
    """Write .session_test.py, the copy of test.py that actually runs:
      - kaneai.fresh_session_per_run: every quoted URL on the target host (goto
        literals and recorded start_url variable values) moves into one new,
        empty /s/<id>/ session;
      - kaneai.yes_no_compat (default on): testmu_compat is imported first.
    Returns the script to run ("test.py" when neither applies)."""
    src = (cwd / "test.py").read_text(encoding="utf-8")
    out, changed = src, False

    target = os.environ.get("TARGET_URL") or _config_value("target_url")
    origin = re.match(r"^https?://[^/\s]+", target or "")
    if _config_value("fresh_session_per_run").lower() == "true" and origin:
        sid = secrets.token_hex(16)

        def _repoint(m: re.Match) -> str:
            path = re.sub(r"^/s/[0-9a-f]{32}(?=/|$)", "", m.group(3) or "") or "/"
            return f"{m.group(1)}{m.group(2)}/s/{sid}{'/tasks' if path == '/' else path}{m.group(4)}"

        # Every quoted target URL: page.goto("…") literals, and the recorded value
        # of a {{start_url}} variable in testmu.configure(global_variables=[…]).
        pattern = re.compile(r"([\"'])(" + re.escape(origin.group(0)) + r")(/[^\"']*)?([\"'])")
        out, n = pattern.subn(_repoint, out)
        changed = True
        print(f"[native] fresh session /s/{sid}/ ({n} navigation(s) repointed)")

    if _config_value("select_option_repair").lower() != "false":
        out, n = _repair_select_options(out)
        if n:
            print(f"[native] {n} select_option() call(s) given the value from their step label")
            changed = True

    if _config_value("yes_no_compat").lower() != "false":
        out = _COMPAT_HEADER + out
        changed = True

    if not changed:
        return "test.py"
    (cwd / ".session_test.py").write_text(out, encoding="utf-8")
    return ".session_test.py"


def _run_test(env: dict, script: str = "test.py") -> tuple[int, str]:
    """Stream the test's combined output to stdout while capturing it."""
    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    chunks: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        chunks.append(line)
    proc.wait()
    return proc.returncode, "".join(chunks)


def _write_junit(sc_id: str, status: str, duration: float, message: str, output: str) -> Path:
    if status == "failed":
        body = "    <failure message={}>{}</failure>\n".format(quoteattr(message), escape(output[-OUTPUT_TAIL:]))
    elif status == "skipped":
        body = "    <skipped message={}/>\n".format(quoteattr(message))
    else:
        body = ""
    path = REPORTS / f"native_{sc_id}.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="native" tests="1" failures="{failures}" errors="0" skipped="{skipped}" time="{time:.3f}">\n'
        '  <testcase classname="native" name={name} time="{time:.3f}">\n'
        '{body}'
        '  </testcase>\n'
        '</testsuite>\n'.format(
            failures=1 if status == "failed" else 0,
            skipped=1 if status == "skipped" else 0,
            time=duration,
            name=quoteattr(sc_id),
            body=body,
        ),
        encoding="utf-8",
    )
    return path


def _write_result_json(sc_id: str, status: str, start: datetime, end: datetime,
                       duration_ms: int, error_message: str | None, session_link: str) -> Path:
    tc_id = f"TC-{sc_id.split('-')[1]}" if "-" in sc_id else "TC-000"
    build = _build_name()
    record = {
        "requirement_id": _requirement_id(sc_id) or "unknown",
        "scenario_id": sc_id,
        "test_case_id": tc_id,
        "function_name": "test.py",
        "browser": BROWSER,
        "lt_browser": LT_BROWSER,
        "status": status,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "duration_ms": duration_ms,
        "error_message": error_message,
        "session_name": f"{sc_id} | {tc_id} | native | {BROWSER}",
        "session_link": session_link,
        "build": build,
        "source": "native",
    }
    path = REPORTS / f"kane_result_{sc_id}_{BROWSER}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def main() -> int:
    cwd = Path.cwd()
    sc_id = _sc_id_from_folder(cwd.name)
    REPORTS.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.setdefault("TESTMU_HEADLESS", "true")
    env.setdefault("PYTHONUNBUFFERED", "1")

    print(f"[native] {sc_id}: running {cwd / 'test.py'} (TESTMU_HEADLESS={env['TESTMU_HEADLESS']})")
    start = datetime.now(timezone.utc)
    start_mono = time.monotonic()

    if not (cwd / "test.py").is_file():
        rc, output = 2, f"[native] {sc_id}: test.py not found in {cwd}\n"
        sys.stdout.write(output)
    elif not _ASSERT_RE.search((cwd / "test.py").read_text(encoding="utf-8", errors="replace")):
        rc, output = 1, (f"[native] {sc_id}: export has no assertion (no verify_assertion / expect) — "
                         "reported failed, a test that checks nothing is not a pass\n")
        sys.stdout.write(output)
    else:
        script = _session_copy(cwd)
        try:
            rc, output = _run_test(env, script)
        finally:
            if script != "test.py":
                (cwd / script).unlink(missing_ok=True)

    duration = time.monotonic() - start_mono
    end = datetime.now(timezone.utc)
    duration_ms = round(duration * 1000)

    if rc == 0:
        status, message, error_message = "passed", "", None
    elif rc == SKIP_EXIT_CODE:
        status, message, error_message = "skipped", "skipped by native stub (export missing)", None
    else:
        status = "failed"
        message = f"exit code {rc}"
        tail = output.strip()[-ERROR_TAIL:]
        error_message = f"{message}: {tail}" if tail else message

    link_match = _LINK_RE.search(output)
    session_link = link_match.group(0) if link_match else ""

    xml_path = _write_junit(sc_id, status, duration, message, output)
    json_path = _write_result_json(sc_id, status, start, end, duration_ms, error_message, session_link)
    print(f"[native] {sc_id}: {status.upper()} in {duration_ms} ms (exit {rc}) -> "
          f"{xml_path.relative_to(REPO_ROOT).as_posix()}, {json_path.relative_to(REPO_ROOT).as_posix()}")

    # Propagate the verdict — HyperExecute gates the task on the exit code. A skip is not a failure.
    return 0 if status == "skipped" else rc


if __name__ == "__main__":
    sys.exit(main())
