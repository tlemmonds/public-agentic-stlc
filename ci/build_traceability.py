"""
Build traceability matrix from real execution artifacts only.

Data sources (all real, no fabrication):
  - requirements/analyzed_requirements.json  — Kane AI functional verification (Stage 1)
  - scenarios/scenarios.json                 — scenario catalog with function_name
  - reports/normalized_results.json          — normalized Playwright results (all browsers)
  - reports/api_details.json                 — HE session links (fallback for session URLs)
  - reports/junit-*.xml / reports/junit.xml  — raw pytest results (fallback when conftest absent)
  - reports/test_execution_manifest.json     — run type (full/incremental)

Many-to-one: a requirement may own several scenarios.
  - `rows`         — ONE PER SCENARIO (plus one "n/a" row for an uncovered requirement,
                     plus tombstone rows for deprecated scenarios).
  - `requirements` — one per requirement: scenario ids + Kane/Playwright roll-up + overall.
  - `summary`      — executed/passed/pass_rate are scenario-level counts over rows;
                     requirements_covered / untested_requirements are requirement-level.

When data is missing: marks as "data_unavailable". Never fabricates values.
"""
import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import current_release, load_run_validity, print_stage_header, print_stage_result, run_banner_lines
from project_config import (
    classify_feature,
    criticality_for,
    deprecated_by_requirement,
    group_scenarios_by_requirement,
    rollup_kane_status,
    rollup_playwright_status,
)

_NEGATIVE_KW = frozenset(["invalid", "error", "fail", "reject", "empty", "remove",
                           "delete", "cannot", "unauthorized", "no results"])
_EDGE_KW     = frozenset(["empty cart", "zero", "boundary", "duplicate", "persistence"])

DEBUG = os.environ.get("REPORT_DEBUG", "false").lower() == "true"


def _debug(msg):
    if DEBUG:
        print(f"[REPORT_DEBUG] {msg}")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", default="requirements/analyzed_requirements.json")
    parser.add_argument("--scenarios", default="scenarios/scenarios.json")
    parser.add_argument("--manifest", default="reports/test_execution_manifest.json")
    parser.add_argument("--normalized", default="reports/normalized_results.json")
    parser.add_argument("--api-details", default="reports/api_details.json")
    parser.add_argument("--out", default="reports/traceability_matrix.md")
    parser.add_argument("--json-out", default="reports/traceability_matrix.json")
    return parser.parse_args()


def load_json(path, default):
    p = Path(path)
    if not p.exists():
        _debug(f"File not found: {path}")
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        _debug(f"Failed to parse {path}: {exc}")
        return default


def _load_normalized_results(path):
    """
    Returns:
      results_by_scenario: {sc_id: [record, ...]}  — all browser results for each scenario
      all_browsers: sorted list of browsers seen across all results
    """
    raw = load_json(path, {})
    results = raw.get("results", [])
    by_sc: dict = {}
    browsers = set()
    for r in results:
        sc_id = r.get("scenario_id", "")
        by_sc.setdefault(sc_id, []).append(r)
        browsers.add(r.get("browser", "chrome"))
    _debug(f"Normalized results: {len(results)} records, {len(browsers)} browser(s): {sorted(browsers)}")
    return by_sc, sorted(browsers)


def _archived_results(release_dir: Path = Path("release_notes")) -> list[tuple[str, dict]]:
    """[(release, {sc_id: [records]})] from each archived release's
    reports/normalized_results.json, newest release first. Only results that
    were executed in that run are kept (never ones it carried forward)."""
    import re as _re

    def _key(path: Path):
        m = _re.match(r"v(\d+)\.(\d+)\.(\d+)$", path.parent.parent.name)
        return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)

    out = []
    for f in sorted(release_dir.glob("v*/reports/normalized_results.json"), key=_key, reverse=True):
        by_sc: dict = {}
        for r in load_json(f, {}).get("results", []):
            if (r.get("source") != "carried_forward" and r.get("scenario_id")
                    and r.get("status") != "data_unavailable"):
                by_sc.setdefault(r["scenario_id"], []).append(r)
        out.append((f.parent.parent.name, by_sc))
    return out


def _carried_forward(sc_id: str, archives: list[tuple[str, dict]]) -> tuple[str, list[dict]]:
    """The latest archived regression result for a scenario this run did not
    select: (release, records marked carried_forward), else ("", [])."""
    for release, by_sc in archives:
        records = by_sc.get(sc_id)
        if records:
            return release, [dict(r, source="carried_forward", carried_from=release) for r in records]
    return "", []


def _junit_fallback(junit_glob="reports"):
    """Last-resort: parse junit XML files directly. Returns {fn_name: {status, duration_ms}}."""
    results = {}
    reports_dir = Path(junit_glob)
    if not reports_dir.exists():
        return results
    for xml_file in sorted(reports_dir.glob("junit*.xml")):
        try:
            root = ET.fromstring(xml_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for testcase in root.iter("testcase"):
            name = testcase.attrib.get("name", "")
            duration_ms = round(float(testcase.attrib.get("time", "0") or "0") * 1000)
            if testcase.find("failure") is not None or testcase.find("error") is not None:
                status = "failed"
            elif testcase.find("skipped") is not None:
                status = "skipped"
            else:
                status = "passed"
            results[name] = {"status": status, "duration_ms": duration_ms}
    return results


def _overall_status(browser_records: list) -> str:
    """Aggregate across browsers: failed > skipped > passed (any) > data_unavailable.

    A test is PASSED if at least one browser passed and none failed.
    data_unavailable from secondary browsers does not block a pass verdict —
    it would incorrectly penalise tests where only one shard's artifact is missing.
    """
    if not browser_records:
        return "data_unavailable"
    statuses = [r.get("status", "data_unavailable") for r in browser_records]
    if "failed" in statuses:
        return "failed"
    if "passed" in statuses:
        return "passed"
    if "skipped" in statuses:
        return "skipped"
    return "data_unavailable"


def _browser_summary(browser_records: list, all_browsers: list) -> dict:
    """Returns {browser: status} for table display."""
    by_browser = {r["browser"]: r.get("status", "data_unavailable") for r in browser_records}
    return {b: by_browser.get(b, "data_unavailable") for b in all_browsers}


def _kane_from(record: dict, default_status: str) -> dict:
    return {
        "status":    record.get("kane_status") or default_status,
        "links":     record.get("kane_links") or [],
        "case_link": record.get("kane_case_link", ""),
        "one_liner": record.get("kane_one_liner", ""),
        "summary":   record.get("kane_summary", ""),
        "steps":     record.get("kane_steps") or [],
    }


def _kane_for_scenario(req: dict, sc_id: str) -> dict:
    """Kane evidence for one scenario of a requirement.

    Prefers the per-scenario entry in `req["scenarios"]` (many-to-one Stage 1
    output). When that list is absent the file is a legacy 1:1 record and the
    requirement-level fields are used. When the list is present but has no
    entry for this scenario, the scenario has not been run by Kane.
    """
    entries = req.get("scenarios")
    if isinstance(entries, list):
        entry = next(
            (e for e in entries if isinstance(e, dict) and e.get("scenario_id") == sc_id),
            None,
        )
        if entry is None:
            return {"status": "not_run", "links": [], "one_liner": "", "summary": "", "steps": []}
        return _kane_from(entry, "unknown")
    return _kane_from(req, "unknown")


def _requirement_overall(active_rows: list) -> str:
    """passed iff ≥1 executed, every executed scenario passed, Kane roll-up passed."""
    executed_rows = [r for r in active_rows if r["playwright_status"] != "data_unavailable"]
    if not executed_rows:
        return "data_unavailable"
    kane_rollup = rollup_kane_status(r["kane_ai_result"] for r in active_rows)
    if kane_rollup == "passed" and all(r["overall"] == "passed" for r in executed_rows):
        return "passed"
    return "failed"


def compute_result_analysis(rows, summary, all_browsers, requirements_rollup=None):
    """Kane pass rate stays scenario-level (over rows); failed requirements and
    key findings are keyed per requirement using the roll-up list."""
    requirements_rollup = requirements_rollup or []
    kane_passed = sum(1 for r in rows if r.get("kane_ai_result") == "passed")
    kane_ran = sum(1 for r in rows if r.get("kane_ai_result") in ("passed", "failed"))
    kane_pass_rate = round((kane_passed / kane_ran) * 100, 1) if kane_ran else 0.0

    pw_pass_rate = summary.get("pass_rate", 0.0)

    live_reqs = [q for q in requirements_rollup if q.get("overall") != "deprecated"]

    failed_reqs = sorted({
        q["requirement_id"]
        for q in live_reqs
        if q.get("kane_status") == "failed" or q.get("playwright_status") == "failed"
    })

    key_findings = []
    for q in live_reqs:
        req_id = q["requirement_id"]
        kane_fail = q.get("kane_status") == "failed"
        pw_fail = q.get("playwright_status") == "failed"
        n_sc = len(q.get("scenario_ids", []))
        suffix = f" ({n_sc} scenarios)" if n_sc > 1 else ""
        if kane_fail and pw_fail:
            key_findings.append(f"{req_id}{suffix}: failed both Kane AI verification and Playwright regression.")
        elif kane_fail:
            key_findings.append(
                f"{req_id}{suffix}: failed Kane AI verification; Playwright status is {q.get('playwright_status', 'unknown')}."
            )
        elif pw_fail:
            key_findings.append(f"{req_id}{suffix}: passed Kane AI verification but failed Playwright regression.")

    unavailable = [q for q in live_reqs if q.get("playwright_status") == "data_unavailable"]
    if unavailable:
        key_findings.append(
            f"{len(unavailable)} requirement(s) have no Playwright execution data (data_unavailable)."
        )
    uncovered = [q for q in live_reqs if not q.get("scenario_ids")]
    if uncovered:
        key_findings.append(
            f"{len(uncovered)} requirement(s) have no scenario at all: "
            + ", ".join(q["requirement_id"] for q in uncovered) + "."
        )

    if not key_findings:
        key_findings.append(
            "All tested requirements passed both Kane AI verification and Playwright regression."
        )

    has_failures = bool(failed_reqs)
    if pw_pass_rate >= 90 and not has_failures:
        risk_level = "low"
    elif pw_pass_rate >= 75:
        risk_level = "medium"
    else:
        risk_level = "high"

    overall_health = {"low": "healthy", "medium": "at_risk", "high": "critical"}[risk_level]

    untested = summary.get("untested_requirements", [])
    if overall_health == "healthy":
        recommendation_hint = (
            "All requirements passed verification and regression across all browsers; "
            "release can proceed with confidence."
        )
    elif untested:
        recommendation_hint = (
            f"Release blocked: {len(failed_reqs)} failing requirement(s) and "
            f"{len(untested)} with no execution data. Resolve before shipping."
        )
    else:
        recommendation_hint = (
            f"Release carries medium risk: {len(failed_reqs)} failing requirement(s). "
            "Review browser-specific failures before conditional approval."
        )

    return {
        "overall_health": overall_health,
        "kane_pass_rate": kane_pass_rate,
        "playwright_pass_rate": pw_pass_rate,
        "risk_level": risk_level,
        "key_findings": key_findings,
        "failed_requirements": failed_reqs,
        "recommendation_hint": recommendation_hint,
        "browsers_tested": all_browsers,
    }


def _empty_categories() -> dict:
    return {
        "happy_path": False, "negative": False, "edge_case": False,
        "he_executed": False, "regression": False,
    }


def main():
    args = parse_args()
    print_stage_header("7", "TRACEABILITY_REPORT", "Build requirement → scenario → test → result matrix")
    Path("reports").mkdir(exist_ok=True)
    requirements = load_json(args.requirements, [])
    if isinstance(requirements, dict):
        requirements = requirements.get("requirements", [])
    scenarios = load_json(args.scenarios, [])
    manifest = load_json(args.manifest, {})

    normalized_by_sc, all_browsers = _load_normalized_results(args.normalized)

    # Incremental run: a scenario that was not selected has no result of its
    # own. Carry its latest archived result forward, labelled with the release
    # it came from, instead of counting it untested (or reading leftovers).
    incremental = manifest.get("run_type") == "incremental"
    selected = set(manifest.get("selected_scenarios", [])) | set(manifest.get("selected_native", []))
    archives = _archived_results() if incremental else []
    carried = 0

    # Fallback: parse junit directly if normalized results are absent
    junit_fallback = {} if normalized_by_sc else _junit_fallback()
    _debug(f"JUnit fallback entries: {len(junit_fallback)}")

    # HE session links for scenarios that have no conftest result
    api_details = load_json(args.api_details, {})
    he_task_links = {
        (t.get("name") or "").strip(): t.get("session_link", "")
        for t in api_details.get("he_tasks", [])
        if t.get("name")
    }

    scenarios_by_req = group_scenarios_by_requirement(scenarios)
    deprecated_by_req = deprecated_by_requirement(scenarios)

    rows = []
    requirements_out = []
    executed = 0
    passed = 0
    untested = []
    failing = []

    for req in requirements:
        req_id = req["id"]
        description = req.get("description", "")
        active_scenarios = scenarios_by_req.get(req_id, [])
        tombstones = deprecated_by_req.get(req_id, [])
        brd_ref = req.get("brd_ref") or next(
            (s.get("brd_ref") for s in active_scenarios + tombstones if s.get("brd_ref")), ""
        ) or ""

        # Deprecated tombstones: render the row for visibility (so the matrix
        # documents that a release explicitly removed the behaviour) but exclude
        # from executed/passed/untested counts — otherwise the missing HE result
        # flips the verdict to RED on every run.
        for scenario in tombstones:
            sc_id = scenario["id"]
            rows.append({
                "requirement_id": req_id,
                "brd_ref": brd_ref,
                "acceptance_criterion": description,
                "scenario_id": sc_id,
                "test_case_id": scenario.get("test_case_id", "n/a"),
                "function_name": scenario.get("function_name", f"test_{sc_id.lower().replace('-', '_')}"),
                "source": scenario.get("source", "generated"),
                "feature": scenario.get("feature", "general"),
                "criticality": "DEPRECATED",
                "coverage_categories": [],
                "kane_ai_result": "skipped",
                "kane_session_link": "",
                "kane_one_liner": f"removed in {scenario.get('deprecated_in_release', 'prior release')}",
                "kane_summary": "",
                "kane_steps": [],
                "playwright_status": "deprecated",
                "playwright_per_browser": {},
                "session_link": "",
                "overall": "deprecated",
            })

        # Uncovered requirement (no non-deprecated scenario): ONE "n/a" row,
        # unless the requirement is documented purely by tombstones (as today).
        if not active_scenarios:
            if not tombstones:
                kane = _kane_from(req, "not_run")
                feature = classify_feature(description)
                rows.append({
                    "requirement_id": req_id,
                    "brd_ref": brd_ref,
                    "acceptance_criterion": description,
                    "scenario_id": "n/a",
                    "test_case_id": "n/a",
                    "function_name": "",
                    "source": "n/a",
                    "feature": feature,
                    "criticality": criticality_for(feature, requirement_id=req_id, brd_ref=brd_ref),
                    "coverage_categories": _empty_categories(),
                    "kane_ai_result": kane["status"],
                    "kane_session_link": kane["links"][0] if kane["links"] else "",
                "kane_case_link": kane["case_link"],
                    "kane_one_liner": kane["one_liner"],
                    "kane_summary": kane["summary"],
                    "kane_steps": kane["steps"],
                    "playwright_status": "data_unavailable",
                    "playwright_per_browser": {b: "data_unavailable" for b in all_browsers},
                    "session_link": "",
                    "overall": "data_unavailable",
                })
                untested.append(req_id)
                req_overall = "data_unavailable"
            else:
                req_overall = "deprecated"
            requirements_out.append({
                "requirement_id": req_id,
                "brd_ref": brd_ref,
                "acceptance_criterion": description,
                "scenario_ids": [],
                "deprecated_scenario_ids": [s["id"] for s in tombstones],
                "kane_status": rollup_kane_status([]),
                "playwright_status": rollup_playwright_status([]),
                "overall": req_overall,
            })
            _debug(f"{req_id}: no active scenario → {req_overall}")
            continue

        req_rows = []
        for scenario in active_scenarios:
            sc_id = scenario["id"]
            tc_id = scenario.get("test_case_id", "n/a")
            fn_name = scenario.get("function_name", f"test_{sc_id.lower().replace('-', '_')}")

            # ── Playwright results (multi-browser) ───────────────────────────
            browser_records = normalized_by_sc.get(sc_id, [])

            if not browser_records and fn_name and fn_name in junit_fallback:
                jdata = junit_fallback[fn_name]
                browser_records = [{
                    "scenario_id": sc_id,
                    "browser": "chrome",
                    "status": jdata["status"],
                    "duration_ms": jdata["duration_ms"],
                    "session_link": he_task_links.get(fn_name, ""),
                    "source": "junit",
                }]

            carried_from = ""
            # normalize_artifacts writes a data_unavailable placeholder for
            # every scenario without a result, so "no records" isn't enough.
            has_result = any(r.get("status") != "data_unavailable" for r in browser_records)
            if not has_result and incremental and sc_id not in selected:
                carried_from, browser_records = _carried_forward(sc_id, archives)
                if browser_records:
                    carried += 1
                    for b in {r.get("browser", "chrome") for r in browser_records}:
                        if b not in all_browsers:
                            all_browsers = sorted(set(all_browsers) | {b})

            playwright_overall = _overall_status(browser_records)
            per_browser = _browser_summary(browser_records, all_browsers)

            session_link = next(
                (r.get("session_link", "") for r in browser_records if r.get("session_link")),
                he_task_links.get(fn_name, ""),
            )

            # ── Kane AI result (Stage 1, per scenario) ───────────────────────
            kane = _kane_for_scenario(req, sc_id)
            kane_status = kane["status"]

            # ── Combined overall — BOTH Kane AND Playwright must pass ────────
            if playwright_overall != "data_unavailable":
                executed += 1
                if playwright_overall == "passed" and kane_status == "passed":
                    overall = "passed"
                    passed += 1
                else:
                    overall = "failed"
                    failing.append(sc_id)
            else:
                overall = "data_unavailable"

            _debug(
                f"{req_id}/{sc_id}: kane={kane_status} playwright={playwright_overall} "
                f"overall={overall} browsers={per_browser}"
            )

            feature = classify_feature(description, explicit=scenario.get("feature"))
            criticality = criticality_for(feature, requirement_id=req_id, brd_ref=brd_ref)
            combined_txt = f"{description} {scenario.get('title', '')} {scenario.get('description', '')}".lower()
            tags = {str(t).lower() for t in (scenario.get("tags") or [])}
            coverage_categories = {
                "happy_path":  True,
                "negative":    "negative" in tags or any(kw in combined_txt for kw in _NEGATIVE_KW),
                "edge_case":   bool(tags & {"edge", "boundary"}) or any(kw in combined_txt for kw in _EDGE_KW),
                "he_executed": bool(session_link),
                "regression":  playwright_overall not in ("data_unavailable", None),
            }

            row = {
                "requirement_id": req_id,
                "brd_ref": brd_ref,
                "acceptance_criterion": description,
                "scenario_id": sc_id,
                "test_case_id": tc_id,
                "function_name": fn_name,
                "source": scenario.get("source", "generated"),
                "feature": feature,
                "criticality": criticality,
                "coverage_categories": coverage_categories,
                "kane_ai_result": kane_status,
                "kane_session_link": kane["links"][0] if kane["links"] else "",
                "kane_case_link": kane["case_link"],
                "kane_one_liner": kane["one_liner"],
                "kane_summary": kane["summary"],
                "kane_steps": kane["steps"],
                "playwright_status": playwright_overall,
                "playwright_per_browser": per_browser,
                "playwright_carried_from": carried_from,
                "session_link": session_link,
                "overall": overall,
            }
            rows.append(row)
            req_rows.append(row)

        req_overall = _requirement_overall(req_rows)
        if req_overall == "data_unavailable":
            untested.append(req_id)
        requirements_out.append({
            "requirement_id": req_id,
            "brd_ref": brd_ref,
            "acceptance_criterion": description,
            "scenario_ids": [r["scenario_id"] for r in req_rows],
            "deprecated_scenario_ids": [s["id"] for s in tombstones],
            "kane_status": rollup_kane_status(r["kane_ai_result"] for r in req_rows),
            "playwright_status": rollup_playwright_status(r["playwright_status"] for r in req_rows),
            "overall": req_overall,
        })

    pass_rate = round((passed / executed) * 100, 1) if executed else 0.0
    untested_to_report = untested if executed > 0 else []

    summary = {
        "run_type": manifest.get("run_type", "unknown"),
        "requirements_covered": sum(1 for q in requirements_out if q["scenario_ids"]),
        "requirements_total": len(requirements),
        "requirements_uncovered": sum(
            1 for q in requirements_out if not q["scenario_ids"] and q["overall"] != "deprecated"
        ),
        "scenarios_total": sum(len(q["scenario_ids"]) for q in requirements_out),
        "executed": executed,
        "executed_this_run": executed - carried,
        "carried_forward": carried,
        "passed": passed,
        "pass_rate": pass_rate,
        "browsers_tested": all_browsers,
        "untested_requirements": untested_to_report,
        "failing_scenarios": [s for s in failing if s != "n/a"],
    }

    result_analysis = compute_result_analysis(rows, summary, all_browsers, requirements_out)

    # ── Markdown ──────────────────────────────────────────────────────────────
    browser_cols = " | ".join(b.capitalize() for b in all_browsers) if all_browsers else "Browser"
    browser_sep = " | ".join("---" for _ in (all_browsers or ["chrome"]))

    lines = [
        "# Traceability Matrix",
        "",
        *run_banner_lines(),
        f"- Release: {current_release() or 'unknown'}",
        f"- Run type: {summary['run_type']}",
        f"- Requirements covered: {summary['requirements_covered']}/{summary['requirements_total']}"
        + (f" ({summary['requirements_uncovered']} uncovered)" if summary["requirements_uncovered"] else ""),
        f"- Scenarios: {summary['scenarios_total']} ({summary['executed_this_run']} executed this run"
        + (f", {summary['carried_forward']} unchanged and carried forward from the last archived release)"
           if summary["carried_forward"] else ")"),
        f"- Browsers tested: {', '.join(all_browsers) or 'none'}",
        f"- Playwright pass rate: {summary['pass_rate']}% "
        f"({summary['passed']} passed, {summary['executed'] - summary['passed']} failed or skipped)",
        "",
        "## Requirement Roll-up",
        "",
        "| Req | BRD ref | Scenarios | Kane | Playwright | Overall |",
        "|---|---|---|---|---|---|",
    ]
    for q in requirements_out:
        sc_cell = ", ".join(q["scenario_ids"]) or "—"
        if q["deprecated_scenario_ids"]:
            sc_cell += f" (deprecated: {', '.join(q['deprecated_scenario_ids'])})"
        lines.append(
            f"| {q['requirement_id']} | {q['brd_ref'] or '—'} | {sc_cell} "
            f"| {q['kane_status']} | {q['playwright_status']} | {q['overall']} |"
        )

    lines.extend([
        "",
        "## Scenario Matrix",
        "",
        f"| Req ID | BRD ref | Acceptance Criterion | Scenario | Test Case | Source | Kane Verify | Kane Session | What Kane Saw | {browser_cols} | Playwright | Session | Overall |",
        f"|---|---|---|---|---|---|---|---|---|{browser_sep}|---|---|---|",
    ])

    for row in rows:
        one_liner = row.get("kane_one_liner") or "—"
        kane_link = row.get("kane_session_link", "")
        kane_cell = f"[session]({kane_link})" if kane_link else "—"
        sel_link = row.get("session_link", "")
        sel_cell = f"[session]({sel_link})" if sel_link else "—"
        per_browser = row.get("playwright_per_browser", {})
        browser_cells = " | ".join(per_browser.get(b, "—") for b in all_browsers) if all_browsers else "—"
        criterion = row["acceptance_criterion"]
        lines.append(
            f"| {row['requirement_id']} "
            f"| {row.get('brd_ref') or '—'} "
            f"| {criterion} "
            f"| {row['scenario_id']} "
            f"| {row['test_case_id']} "
            f"| {row.get('source') or '—'} "
            f"| {row['kane_ai_result']} "
            f"| {kane_cell} "
            f"| {one_liner} "
            f"| {browser_cells} "
            f"| {row['playwright_status']}"
            + (f" (carried from {row['playwright_carried_from']})" if row.get("playwright_carried_from") else "")
            + f" | {sel_cell} "
            f"| {row['overall']} |"
        )

    # Kane AI detail section (per scenario)
    lines.extend(["", "## Kane AI Verification Detail", ""])
    for row in rows:
        lines.append(f"### {row['requirement_id']} / {row['scenario_id']} — {row['acceptance_criterion']}")
        if row.get("kane_one_liner"):
            lines.append(f"> {row['kane_one_liner']}")
        lines.append("")
        if row.get("kane_steps"):
            lines.append("**Steps observed by Kane AI:**")
            lines.extend([f"- {step}" for step in row["kane_steps"]])
            lines.append("")
        if row.get("kane_summary"):
            lines.append(f"**Full summary:** {row['kane_summary']}")
            lines.append("")
        if row.get("kane_session_link"):
            lines.append(
                f"**Session:** [{row['kane_session_link']}]({row['kane_session_link']})"
            )
            lines.append("")

    kane_only_issues = [
        r for r in rows
        if r["playwright_status"] == "passed" and r["kane_ai_result"] == "failed"
    ]
    if kane_only_issues:
        lines.extend(["", "## Kane Analysis Warnings", ""])
        for r in kane_only_issues:
            lines.append(
                f"- {r['scenario_id']}: Kane returned `failed` while Playwright passed."
            )

    if summary["untested_requirements"]:
        lines.extend(["", "## No Execution Data", ""])
        uncovered_ids = {q["requirement_id"] for q in requirements_out if not q["scenario_ids"]}
        for item in summary["untested_requirements"]:
            if item in uncovered_ids:
                lines.append(f"- {item}: no scenario covers this requirement")
            else:
                lines.append(f"- {item}: no Playwright execution data (data_unavailable)")

    if summary["failing_scenarios"]:
        lines.extend(["", "## Failing Scenarios", ""])
        for item in summary["failing_scenarios"]:
            lines.append(f"- {item}")

    lines.extend(["", "## Result Analysis", ""])
    lines.extend([
        f"- **Overall health:** {result_analysis['overall_health']}",
        f"- **Risk level:** {result_analysis['risk_level']}",
        f"- **Kane AI pass rate:** {result_analysis['kane_pass_rate']}%",
        f"- **Playwright pass rate:** {result_analysis['playwright_pass_rate']}%",
        f"- **Browsers tested:** {', '.join(result_analysis['browsers_tested']) or 'none'}",
        "",
    ])
    if result_analysis["failed_requirements"]:
        lines.append("**Failed requirements:**")
        lines.extend([f"- {r}" for r in result_analysis["failed_requirements"]])
        lines.append("")
    lines.append("**Key findings:**")
    lines.extend([f"- {f}" for f in result_analysis["key_findings"]])
    lines.extend(["", f"**Recommendation:** {result_analysis['recommendation_hint']}", ""])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_path = Path(args.json_out)
    json_path.write_text(
        json.dumps({
            "summary": summary,
            "requirements": requirements_out,
            "rows": rows,
            "result_analysis": result_analysis,
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    failing_count = summary.get("executed", 0) - summary.get("passed", 0)
    print_stage_result("7", "TRACEABILITY_REPORT", {
        "Requirements covered": f"{summary['requirements_covered']}/{summary['requirements_total']}",
        "Scenarios":            f"{summary['scenarios_total']} ({summary['executed']} executed)",
        "Pass rate":            f"{pass_rate}%",
        "Passed":               summary.get("passed", 0),
        "Failed":               failing_count,
        "Untested":             len(summary.get("untested_requirements", [])),
        "Browsers":             ", ".join(all_browsers) or "none",
        "Output":               f"{args.out}, {args.json_out}",
    })


if __name__ == "__main__":
    main()
