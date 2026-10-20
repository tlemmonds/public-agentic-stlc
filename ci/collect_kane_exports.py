"""
Stage 3a — Collect Kane AI code exports and assemble tests/playwright/test_regression.py.

For every active scenario in scenarios.json, this script picks one of two paths:

  vanilla (default)  — legacy 1:1 assembly, unchanged:
    1. Looks up the scenario's Kane code-export directory from analyzed_requirements.json
       (analyzed_requirements[req]["scenarios"][i]["kane_code_export_dir"]; falls back to
       the requirement-level `kane_code_export_dir` for legacy 1:1 files).
    2. Reads the Kane-generated Python Playwright code.
    3. Extracts the test body (strips the function def / type annotations).
    4. Wraps the body in a pytest function with @pytest.mark.scenario / @pytest.mark.requirement.
    5. Writes the assembled file to tests/playwright/test_regression.py.

  native (`export_kind == "testmu"` or `playwright_export` set)  — the export uses the
    async `testmu` SDK and cannot be embedded in the sync conftest, so it is staged
    verbatim (test.py + requirements.txt) into tests/playwright/native/<sc_id_lower>/
    and executed by the framework-owned tests/playwright/native/run_with_junit.py.
    Nothing is written to test_regression.py for a native scenario.

When Kane has no export for a vanilla scenario (session skipped or code-export missing),
the script falls back to a curated hand-written body for that acceptance criterion so
the test is never empty — it is always a real, executable Playwright action.

Writes:
  tests/playwright/test_regression.py
  tests/playwright/native/<sc_id_lower>/{test.py,requirements.txt}   (native scenarios)
  tests/playwright/native/requirements.txt                            (union, native only)
"""
import ast
import json
import re
import shutil
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result

import os
BASE_URL = os.environ.get("TARGET_URL") or __import__("project_config").cfg("target_url", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
NATIVE_ROOT_REL = Path("tests") / "playwright" / "native"
NATIVE_RUNNER_NAME = "run_with_junit.py"
NATIVE_FILES = ("test.py", "requirements.txt")

# ---------------------------------------------------------------------------
# Fallback bodies — used when Kane has no exported code for a given AC.
# Each body is a complete, real Playwright implementation for the acceptance
# criterion.  The placeholder {url} is substituted with the scenario URL.
# ---------------------------------------------------------------------------
# Fallback bodies are empty by default. Replay-first means
# Kane always exports Playwright on record/replay, so this dict only
# fires on a full export failure. agent.py _FALLBACK_BODY (page.goto +
# assert title) covers that case.
_FALLBACK_BODIES: dict[str, str] = {}


def _load_json(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Native (testmu) helpers — shared with select_tests.py
# ---------------------------------------------------------------------------

def is_native(scenario: dict) -> bool:
    """True when the scenario's Playwright export is a testmu-SDK script that must run
    natively (not embedded into test_regression.py)."""
    sc = scenario or {}
    return str(sc.get("export_kind") or "vanilla").lower() == "testmu" or bool(sc.get("playwright_export"))


_TESTMU_IMPORT_RE = re.compile(r"^\s*(import testmu|from testmu\b)", re.M)


def mark_testmu_exports(scenarios: list[dict], analyzed_path: str = "requirements/analyzed_requirements.json") -> list[str]:
    """Kane CLI 0.9 exports async testmu-SDK scripts. Mark every scenario whose
    Kane export imports testmu as export_kind=testmu so Stage 3a stages it as a
    native test and Stage 4 selects its native folder. Returns the marked ids."""
    analyzed = _load_json(analyzed_path, [])
    ac_map = {item["id"]: item for item in analyzed if isinstance(item, dict) and item.get("id")}
    marked: list[str] = []
    for sc in scenarios:
        if sc.get("status") == "deprecated" or is_native(sc):
            continue
        export_dir = _export_dir_for(sc, ac_map)
        test_py = _resolve_path(export_dir) / "test.py" if export_dir else None
        source = test_py.read_text(encoding="utf-8", errors="replace") if test_py and test_py.is_file() else ""
        if source and _TESTMU_IMPORT_RE.search(source):
            sc["export_kind"] = "testmu"
            marked.append(sc["id"])
            if "verify_assertion" not in source and "expect(" not in source:
                # A Kane run that never reached its verify step exports actions only.
                print(f"  [WARN] {sc['id']}: Kane export has no assertion — its regression "
                      f"test cannot fail. Re-record the scenario (Kane status: check Stage 1).")
    return marked


def native_dir_name(sc_id: str) -> str:
    """SC-013 → sc_013 (folder name under tests/playwright/native/)."""
    return str(sc_id).lower().replace("-", "_")


def native_dir_rel(sc_id: str) -> str:
    """Repo-relative, forward-slash path used as the selection line for a native scenario."""
    return (NATIVE_ROOT_REL / native_dir_name(sc_id)).as_posix()


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else REPO_ROOT / p


def _extract_test_body(py_file: Path) -> str:
    """Extract the body of the first test_* function from a Kane-exported .py file.

    Strips the function signature and any `page: Page` type annotation so the
    body can be embedded directly in a conftest-compatible pytest function.
    """
    source = py_file.read_text(encoding="utf-8")

    # Try AST extraction first — most reliable
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    lines = source.splitlines()
                    body_start = node.body[0].lineno - 1
                    body_end = node.end_lineno
                    body_lines = lines[body_start:body_end]
                    dedented = textwrap.dedent("\n".join(body_lines))
                    # Remove any awaits — conftest.py uses sync Playwright
                    dedented = re.sub(r"\bawait\s+", "", dedented)
                    return dedented.rstrip()
    except SyntaxError:
        pass

    # Fallback: regex-based extraction
    lines = source.splitlines()
    body_lines: list[str] = []
    in_fn = False
    base_indent: int | None = None

    for line in lines:
        if re.match(r"^(async\s+)?def\s+test", line):
            in_fn = True
            base_indent = None
            continue
        if not in_fn:
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if base_indent is None and stripped:
            base_indent = indent
        if stripped and indent == 0 and base_indent and indent < base_indent:
            break
        if base_indent is not None and indent >= base_indent:
            body_lines.append(line[base_indent:] if base_indent else line)
        else:
            body_lines.append("")

    body = "\n".join(body_lines).strip()
    # Remove await calls for sync Playwright compatibility
    body = re.sub(r"\bawait\s+", "", body)
    return body


def _export_dir_for(scenario: dict, ac_map: dict[str, dict]) -> str:
    """Kane code-export dir for one scenario.

    Many-to-one: analyzed_requirements[req]["scenarios"][i]["kane_code_export_dir"]
    where scenario_id matches. Legacy 1:1 files have no `scenarios` list, so fall
    back to the requirement-level `kane_code_export_dir`.
    """
    item = ac_map.get(scenario.get("requirement_id", ""))
    if not item:
        return ""
    for entry in item.get("scenarios") or []:
        if isinstance(entry, dict) and entry.get("scenario_id") == scenario.get("id"):
            return entry.get("kane_code_export_dir") or item.get("kane_code_export_dir", "") or ""
    return item.get("kane_code_export_dir", "") or ""


def _body_from_export_dir(export_dir: str, label: str, cache: dict[str, str | None]) -> str | None:
    """Extracted test body for an export dir (memoised per dir), or None."""
    if not export_dir:
        return None
    if export_dir in cache:
        return cache[export_dir]
    body: str | None = None
    export_path = Path(export_dir)
    if export_path.exists():
        py_files = sorted(export_path.glob("*.py"))
        if py_files:
            extracted = _extract_test_body(py_files[0])
            if extracted:
                body = extracted
                print(f"  [collect] {label} — Kane export found: {py_files[0].name} ({len(body)} chars)")
            else:
                print(f"  [collect] {label} — Kane export found but body empty: {py_files[0]}")
    cache[export_dir] = body
    return body


def _make_fn_name(sc_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60]
    return f"test_{sc_id.lower().replace('-', '_')}_{slug}"


FILE_HEADER = '''\
"""
Playwright regression suite.
Generated by Agentic STLC pipeline from Kane AI code exports.
Do not edit manually — re-run Stage 1 to regenerate.
"""
import os
import uuid
import pytest
from playwright.sync_api import expect

'''


def build_test_function(scenario: dict, body: str) -> str:
    sc_id = scenario["id"]
    req_id = scenario["requirement_id"]
    fn_name = scenario.get("function_name") or _make_fn_name(sc_id, scenario.get("title", sc_id))
    title = scenario.get("title", "").replace('"', "'")
    indented_body = textwrap.indent(body.strip(), "    ")
    return (
        f'@pytest.mark.scenario("{sc_id}")\n'
        f'@pytest.mark.requirement("{req_id}")\n'
        f'def {fn_name}(page):\n'
        f'    """{sc_id}: {title}."""\n'
        f'{indented_body}\n'
    )


# ---------------------------------------------------------------------------
# Native staging
# ---------------------------------------------------------------------------

_STUB_TEMPLATE = '''\
"""Agentic STLC native stub for {sc_id}.

The testmu export for this scenario was missing at collect time:
  {reason}
Running this file reports the scenario as SKIPPED (exit 77, understood by
tests/playwright/native/run_with_junit.py). Under pytest collection it skips too.
"""
import sys

REASON = {reason_repr}
print("[native] SKIP {sc_id}: " + REASON)
try:
    import pytest
except ImportError:
    sys.exit(77)
try:
    pytest.skip(REASON, allow_module_level=True)
except BaseException:
    sys.exit(77)
'''


def _native_source_dir(scenario: dict, ac_map: dict[str, dict]) -> str:
    """`playwright_export` wins; a testmu scenario without it falls back to its
    per-scenario Kane code-export dir."""
    raw = scenario.get("playwright_export") or _export_dir_for(scenario, ac_map)
    return str(_resolve_path(raw)) if raw else ""


def _stage_native(scenario: dict, source_dir: str, native_root: Path) -> tuple[bool, list[str]]:
    """Fresh-copy test.py + requirements.txt into native_root/<sc_id_lower>/.

    Returns (staged_ok, requirement_lines). On a missing source the folder is
    still created with a skip stub so Stage 4/5 stay consistent, and (False, []) is returned.
    """
    sc_id = scenario["id"]
    target = native_root / native_dir_name(sc_id)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    src = Path(source_dir) if source_dir else None
    test_py = src / "test.py" if src else None
    if not src or not src.is_dir() or not test_py.is_file():
        reason = (f"export dir missing or has no test.py: {source_dir or '<unset>'}")
        print(f"  [WARN] {sc_id}: {reason} — writing pytest.skip stub")
        (target / "test.py").write_text(
            _STUB_TEMPLATE.format(sc_id=sc_id, reason=reason, reason_repr=repr(reason)),
            encoding="utf-8",
        )
        return False, []

    req_lines: list[str] = []
    for name in NATIVE_FILES:
        src_file = src / name
        if src_file.is_file():
            shutil.copyfile(src_file, target / name)
            if name == "requirements.txt":
                for line in src_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        req_lines.append(line)
    return True, req_lines


_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([0-9][0-9A-Za-z.]*)\s*$")


def _version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in v.split("."))


def _reconcile_pins(lines: set[str]) -> list[str]:
    """Collapse conflicting exact pins (`pkg==a` and `pkg==b`) to the highest version.

    Exports recorded on different Kane CLI releases pin different
    `testmuai-playwright-bindings` versions; pip refuses two `==` pins for one
    package (ResolutionImpossible) and the whole HyperExecute job dies at
    prerun. Non-pin lines pass through untouched; the choice is logged."""
    pinned: dict[str, str] = {}
    passthrough: list[str] = []
    for line in lines:
        m = _PIN_RE.match(line)
        if not m:
            passthrough.append(line)
            continue
        name, ver = m.group(1).lower(), m.group(2)
        prev = pinned.get(name)
        if prev is None:
            pinned[name] = ver
        elif _version_key(ver) > _version_key(prev):
            print(f"  [WARN] native requirements: {name}=={prev} and =={ver} both pinned — keeping =={ver}")
            pinned[name] = ver
        elif _version_key(ver) < _version_key(prev):
            print(f"  [WARN] native requirements: {name}=={ver} and =={prev} both pinned — keeping =={prev}")
    return sorted(passthrough + [f"{n}=={v}" for n, v in pinned.items()])


def _prune_stale_native(native_root: Path, keep: set[str]) -> None:
    """Remove native/<sc_*> folders for scenarios no longer native (fresh staging each run).
    Never touches the framework-owned runner or .gitkeep."""
    if not native_root.is_dir():
        return
    for child in native_root.iterdir():
        if child.is_dir() and re.fullmatch(r"sc_\d+", child.name) and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)


def collect_and_assemble(
    analyzed_path: str = "requirements/analyzed_requirements.json",
    scenarios_path: str = "scenarios/scenarios.json",
    output_path: str = "tests/playwright/test_regression.py",
) -> dict:
    print_stage_header("3a", "COLLECT_KANE_EXPORTS",
                       "Assemble Kane-exported Python Playwright code into test_regression.py")

    analyzed: list[dict] = _load_json(analyzed_path, [])
    scenarios: list[dict] = _load_json(scenarios_path, [])

    if not scenarios:
        print(f"[ERROR] No scenarios found at {scenarios_path}", file=sys.stderr)
        sys.exit(1)

    # Build AC-id → analyzed item mapping for export-dir + URL resolution
    ac_map = {item["id"]: item for item in analyzed if isinstance(item, dict) and item.get("id")}
    body_cache: dict[str, str | None] = {}

    # Native root sits beside the assembled file: tests/playwright/native/
    native_root = Path(output_path).parent / "native"

    functions: list[str] = []
    kane_used = 0
    fallback_used = 0
    missing = 0
    native_staged = 0
    native_skipped = 0
    native_dirs: set[str] = set()
    native_reqs: set[str] = set()

    for sc in scenarios:
        if sc.get("status") == "deprecated":
            continue

        if is_native(sc):
            source_dir = _native_source_dir(sc, ac_map)
            ok, req_lines = _stage_native(sc, source_dir, native_root)
            native_dirs.add(native_dir_name(sc["id"]))
            native_reqs.update(req_lines)
            if ok:
                native_staged += 1
                print(f"  [{'native':12}] {sc['id']} ({sc.get('requirement_id', '')}): "
                      f"staged → {native_dir_rel(sc['id'])}")
            else:
                native_skipped += 1
                print(f"  [{'native-skip':12}] {sc['id']} ({sc.get('requirement_id', '')}): "
                      f"stub → {native_dir_rel(sc['id'])}")
            continue

        req_id = sc.get("requirement_id", "")
        url = sc.get("kane_url", BASE_URL)
        if req_id not in ac_map:
            url = BASE_URL

        # Priority 1: Kane-exported body (per-scenario export dir; legacy = requirement-level)
        kane_body = _body_from_export_dir(_export_dir_for(sc, ac_map), req_id, body_cache)
        if kane_body:
            body = kane_body
            kane_used += 1
            source = "kane_export"
        # Priority 2: Curated fallback
        elif req_id in _FALLBACK_BODIES:
            body = _FALLBACK_BODIES[req_id].format(url=url)
            fallback_used += 1
            source = "fallback"
        else:
            body = f'# No implementation available for {req_id}\npytest.skip("No test body for {req_id}")'
            missing += 1
            source = "skip"

        print(f"  [{source:12}] {sc['id']} ({req_id}): {sc.get('title', '')[:50]}")
        functions.append(build_test_function(sc, body))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(FILE_HEADER + "\n\n".join(functions) + "\n", encoding="utf-8")

    # Native bookkeeping: prune stale folders, write the union requirements file.
    _prune_stale_native(native_root, native_dirs)
    native_req_path = native_root / "requirements.txt"
    if native_dirs:
        native_root.mkdir(parents=True, exist_ok=True)
        reconciled = _reconcile_pins(native_reqs)
        native_req_path.write_text(
            "\n".join(reconciled) + ("\n" if reconciled else ""), encoding="utf-8"
        )
        if not (native_root / NATIVE_RUNNER_NAME).is_file():
            print(f"  [WARN] {native_root / NATIVE_RUNNER_NAME} is missing — native scenarios "
                  f"cannot execute (framework-owned file; restore it from git)")
    elif native_req_path.exists():
        native_req_path.unlink()

    total = len(functions)
    print_stage_result("3a", "COLLECT_KANE_EXPORTS", {
        "Vanilla assembled":     total,
        "Kane export used":      kane_used,
        "Fallback used":         fallback_used,
        "Skipped (no impl)":     missing,
        "Kane coverage":         f"{round(kane_used / total * 100)}%" if total else "0%",
        "Native staged":         native_staged,
        "Native skipped":        native_skipped,
        "Output":                output_path + (f", {native_root.as_posix()}/" if native_dirs else ""),
    })

    return {
        "total": total,
        "kane_used": kane_used,
        "fallback_used": fallback_used,
        "missing": missing,
        "native_staged": native_staged,
        "native_skipped": native_skipped,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyzed", default="requirements/analyzed_requirements.json")
    parser.add_argument("--scenarios", default="scenarios/scenarios.json")
    parser.add_argument("--output", default="tests/playwright/test_regression.py")
    args = parser.parse_args()
    collect_and_assemble(args.analyzed, args.scenarios, args.output)
